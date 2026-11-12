"""Per event, per second: each leg's implied width ("vote"), the outlier, and the range trade.

    python votes.py 2026-11-11T22:00          # print the signals for the expiry at that UTC time

Model: log return to expiry r = ln(S_T / S_t) = c * Z, Z ~ standard Student-t with nu degrees of
freedom, centre 0. A leg with line K and Yes price p implies  p = P(Z >= x / c),  x = ln(K / S_t),
so c = x / q with q = t.isf(p, nu).

The first half of this file is pure maths (no data access) and is what the tests exercise.
The second half builds the 1-second table for one event from the recorder's data. Every value
on a grid second is the last one RECEIVED at or before that second, so nothing looks ahead.

Units: tau in seconds. A "vote" v is the width per square-root second, v = c / sqrt(tau_eff),
which is what makes legs with different averaging windows comparable.
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

from recorder.reader import fmt_ts, load

BAND = (0.02, 0.98)              # legs with a mid outside this are dropped
Z_LIMIT = 2.5                    # disagreement, in error bars, that counts as a signal
SCAN_LIMITS = (3.0, 3.5, 4.0, 5.0)     # stricter limits evaluated alongside, see backtest.py section 4
SIGNAL_COLUMNS = ("kind", "outlier", "lo", "hi", "side", "edge", "model")
ANCHOR_ERR = 0.25                # relative error bar on the realised-vol vote
RV_MINUTES = 60                  # realised vol looks back this far, in 1-minute returns
AVG_WINDOW_S = {"5m": 60, "15m": 60, "1h": 0}     # settlement averaging window (FINDINGS.md)
LINE_ERR = {"5m": 0.0, "15m": 0.0, "1h": 0.0}     # measured line error in $, Phase 1: exact
STALENESS_ERR = True             # widen error bars for the age of our spot price (FINDINGS.md, Phase 2)
MAX_FEED_AGE_MS = 5000           # a leg whose reference price is older than this is dropped
NU_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nu.json")


# ============================================================================ maths
def load_nu():
    """{horizon in minutes: nu} as saved by fit_nu.py."""
    with open(NU_FILE) as f:
        return {int(k): float(v) for k, v in json.load(f)["nu_by_horizon_minutes"].items()}


def nu_for(tau_s, nu_by_horizon):
    """nu of the shortest fitted horizon that covers the time left."""
    for minutes in sorted(nu_by_horizon):
        if tau_s <= minutes * 60:
            return nu_by_horizon[minutes]
    return nu_by_horizon[max(nu_by_horizon)]


def tau_eff(tau, w):
    """Effective time left for a leg settling on the average of the last w seconds.

    tau > w: the average of a random walk over its last w seconds has variance tau - 2w/3.
    tau <= w: part of the average is already fixed. Only the remaining share tau/w can still move,
    and it is itself an average over tau seconds (variance tau/3), giving tau^3 / (3 w^2).
    The two meet at tau = w (both give w/3).
    """
    if tau <= 0:
        return 0.0
    if w <= 0:
        return float(tau)
    if tau > w:
        return tau - 2 * w / 3
    return tau ** 3 / (3 * w * w)


def leg_vote(leg, nu):
    """Implied width of one leg, or None if the leg cannot vote.

    leg: dict with name, spot, line, bid, ask, tick, line_err, tau (s), w (averaging window, s).
    Adds mid, x, q, c and sigma (raw width and its error bar), te, v and sv (per sqrt-second).
    """
    mid = (leg["bid"] + leg["ask"]) / 2
    if not (BAND[0] <= mid <= BAND[1]) or not (leg["spot"] > 0 and leg["line"] > 0):   # NaN fails both
        return None
    te = tau_eff(leg["tau"], leg["w"])
    if te <= 0:
        return None
    x = math.log(leg["line"] / leg["spot"])
    q = float(stats.t.isf(mid, nu))
    if q == 0 or x / q <= 0:          # price on the wrong side of 0.5 for where spot is: no width fits
        return None
    c = x / q
    sigma_q = math.hypot(0.5 * leg["tick"] / float(stats.t.pdf(q, nu)), leg["line_err"] / leg["line"] / c)
    sigma = c * sigma_q / abs(q)
    return dict(leg, mid=mid, x=x, q=q, c=c, sigma=sigma, te=te, v=c / math.sqrt(te), sv=sigma / math.sqrt(te))


def anchor_vote(sd, nu):
    """Realised-vol vote. sd: standard deviation of the log price per sqrt-second.

    A Student-t with nu degrees of freedom and scale c has standard deviation c * sqrt(nu / (nu - 2)),
    so the width matching sd is sd * sqrt((nu - 2) / nu).
    """
    if not (sd > 0) or nu <= 2:
        return None
    v = sd * math.sqrt((nu - 2) / nu)
    return {"name": "anchor", "v": v, "sv": ANCHOR_ERR * v}


def weighted_mean(votes):
    """Inverse-variance weighted mean of votes and its standard error."""
    weights = [1 / vote["sv"] ** 2 for vote in votes]
    mean = sum(w * vote["v"] for w, vote in zip(weights, votes)) / sum(weights)
    return mean, 1 / math.sqrt(sum(weights))


def find_outlier(votes, anchor, z_limit=Z_LIMIT):
    """Among 3+ legs: z of each leg against the others plus the anchor, and the outlier if any.

    Returns (outlier vote or None, {name: z}, consensus v, consensus se). When there is an outlier
    the consensus is the mean of the rest; otherwise it is the mean of everything.
    """
    z, chi2, rest_mean = {}, {}, {}
    for vote in votes:
        rest = [other for other in votes if other is not vote] + [anchor]
        mean, se = weighted_mean(rest)
        z[vote["name"]] = (vote["v"] - mean) / math.hypot(vote["sv"], se)
        chi2[vote["name"]] = sum(((other["v"] - mean) / other["sv"]) ** 2 for other in rest)
        rest_mean[vote["name"]] = (mean, se)
    suspects = [vote for vote in votes if abs(z[vote["name"]]) > z_limit]
    if not suspects:
        mean, se = weighted_mean(votes + [anchor])
        return None, z, mean, se
    outlier = min(suspects, key=lambda vote: chi2[vote["name"]])     # removing it leaves the most consistent rest
    mean, se = rest_mean[outlier["name"]]
    return outlier, z, mean, se


def model_prob(vote, v, nu):
    """Model probability that a leg finishes Up, at width v per sqrt-second.

    A vote may carry a "scale" (variants.py, bias correction): its v is then in common units and
    the leg's own width is v * scale. Without one the scale is 1, as in the frozen rules.
    """
    return float(stats.t.sf(vote["x"] / (v * vote.get("scale", 1.0) * math.sqrt(vote["te"])), nu))


def range_edges(lo, hi, v, nu):
    """The range between two legs (lo has the lower strike relative to spot), valued at width v.

    Buying the range = buy Yes on lo, sell Yes on hi (the same as buying No on hi at 1 - bid).
    It pays 1 if lo finishes Up and hi finishes Down. Prices and edges are in dollars per share.
    """
    model = model_prob(lo, v, nu) - model_prob(hi, v, nu)
    buy_cost = lo["ask"] - hi["bid"]
    sell_proceeds = lo["bid"] - hi["ask"]
    return {"lo": lo["name"], "hi": hi["name"], "model": model, "market": lo["mid"] - hi["mid"],
            "edge_buy": model - buy_cost, "edge_sell": sell_proceeds - model}


def signal(legs, sd, nu, z_limit=Z_LIMIT, adjust=None, pair_sigma=None, min_votes=2):
    """Everything for one second. legs: list of leg dicts (see leg_vote). sd: realised vol per sqrt-second.

    kind is "none", "outlier" (3+ legs, one named) or "pair" (2 legs disagree, none named).
    side is "buy" or "sell" (the range) with its executable edge, or None when there is no trade.

    The keyword arguments are for the variants in variants.py; their defaults are the frozen rules.
    adjust(votes) may rescale votes, pair_sigma(a, b) replaces the combined error bar of two votes,
    and min_votes = 3 trades only when three contracts vote.
    """
    out = {"kind": "none", "n_votes": 0, "outlier": None, "z": {}, "consensus": np.nan, "anchor": np.nan,
           "lo": None, "hi": None, "model": np.nan, "market": np.nan, "side": None, "edge": np.nan, "votes": []}
    votes = [vote for vote in (leg_vote(leg, nu) for leg in legs) if vote]
    if adjust:
        votes = adjust(votes)
    votes.sort(key=lambda vote: vote["x"])
    out["votes"], out["n_votes"] = votes, len(votes)
    anchor = anchor_vote(sd, nu)
    if anchor:
        out["anchor"] = anchor["v"]
    if len(votes) < max(2, min_votes):
        return out

    if len(votes) == 2:
        a, b = votes
        gap = abs(a["v"] - b["v"]) / (pair_sigma(a, b) if pair_sigma else math.hypot(a["sv"], b["sv"]))
        out["z"] = {a["name"]: gap, b["name"]: gap}
        if gap <= z_limit:
            return out
        # No leg is named, so value the range twice: once believing each leg. Trade only if both
        # estimates point the same way, and count the smaller edge.
        first, second = range_edges(a, b, a["v"], nu), range_edges(a, b, b["v"], nu)
        out.update(kind="pair", lo=a["name"], hi=b["name"], market=first["market"],
                   model=(first["model"] + second["model"]) / 2)
        for side in ("buy", "sell"):
            edge = min(first[f"edge_{side}"], second[f"edge_{side}"])
            if edge > 0:      # "model" becomes the cautious one of the two values for that side
                pick = min if side == "buy" else max
                out.update(side=side, edge=edge, model=pick(first["model"], second["model"]))
        return out

    if not anchor:
        return out
    outlier, z, consensus, _ = find_outlier(votes, anchor, z_limit)
    out.update(z=z, consensus=consensus)
    if outlier is None:
        return out
    out.update(kind="outlier", outlier=outlier["name"])
    # pair the outlier with the neighbouring strike on spot's side
    i = votes.index(outlier)
    j = i - 1 if outlier["x"] > 0 else i + 1
    if not 0 <= j < len(votes):
        return out
    edges = range_edges(votes[min(i, j)], votes[max(i, j)], consensus, nu)
    side = "buy" if edges["edge_buy"] >= edges["edge_sell"] else "sell"
    out.update(lo=edges["lo"], hi=edges["hi"], model=edges["model"], market=edges["market"],
               side=side, edge=edges[f"edge_{side}"])
    return out


# ============================================================================ recorded data
def load_feeds(start_ms, end_ms):
    """Reference prices received in [start_ms, end_ms), prepared for fast as-of lookups."""
    chainlink = load("chainlink", start_ms, end_ms, columns=["recv_ts", "src_ts", "stream", "value", "backfill"])
    binance = load("binance", start_ms, end_ms, columns=["recv_ts", "open_ts", "open", "close", "trades", "closed", "backfill"])
    spot = chainlink[chainlink.stream == "spot"].sort_values("recv_ts", kind="stable")
    twap = chainlink[chainlink.stream == "twap60"].sort_values("recv_ts", kind="stable").drop_duplicates("src_ts")
    candles = binance[binance.closed].sort_values("recv_ts", kind="stable")
    return {
        "spot_live": spot[~spot.backfill],                       # what we knew spot to be, as it arrived
        "spot_by_src": spot.drop_duplicates("src_ts").sort_values("src_ts"),
        "twap_by_src": twap.set_index("src_ts"),                 # the 5m/15m line is the value stamped at the open
        "candles_live": candles[~candles.backfill],
        "candles_by_open": candles.drop_duplicates("open_ts").set_index("open_ts").sort_index(),
    }


def asof(grid, frame, columns):
    """For each grid time, the last row of `frame` received at or before it."""
    left = pd.DataFrame({"ts": pd.Series(grid, dtype="int64")})
    return pd.merge_asof(left, frame[["recv_ts"] + columns], left_on="ts", right_on="recv_ts", direction="backward")


def realised_sd(grid, candles_by_open, returns=RV_MINUTES, step_s=60):
    """Realised vol per sqrt-second at each grid time, from the last `returns` returns of step_s seconds.

    The default is the 60 one-minute returns before each grid time. Prices are Binance 1-second
    closes. The ladder ends one second back so that the candle it needs has certainly arrived.
    """
    closes = candles_by_open.close
    out = np.full(len(grid), np.nan)
    if closes.empty:
        return out
    first, last = int(grid[0]) - (returns * step_s + 2) * 1000, int(grid[-1])
    series = closes.reindex(range(first, last + 1000, 1000)).ffill(limit=10).values     # index = candle open time
    for n, ts in enumerate(grid):
        end = (int(ts) - 2000 - first) // 1000            # candle that closed at ts - 1 s
        ladder = series[end - returns * step_s:end + 1:step_s]
        if len(ladder) == returns + 1 and np.isfinite(ladder).all():
            out[n] = math.sqrt(np.mean(np.diff(np.log(ladder)) ** 2) / step_s)
    return out


def twap_leg_reference(grid, market, feeds, w):
    """Spot, its age and the line for a leg that settles on the Chainlink 60-second average."""
    spot = asof(grid, feeds["spot_live"], ["src_ts", "value"])
    ref, age = spot.value.values.copy(), (spot.ts - spot.src_ts).values
    line = np.full(len(grid), np.nan)
    if market.open_ts in feeds["twap_by_src"].index:
        row = feeds["twap_by_src"].loc[market.open_ts]
        line[grid >= row.recv_ts] = row.value              # known only once it has arrived
    # Inside the last w seconds part of the settlement average is already fixed. The expected
    # settlement is then (fixed part + remaining share at today's spot).
    by_src = feeds["spot_by_src"]
    window = by_src[(by_src.src_ts > market.close_ts - w * 1000) & (by_src.src_ts <= market.close_ts)]
    for n in np.flatnonzero(grid > market.close_ts - w * 1000):
        seen = window[(window.src_ts <= grid[n]) & (window.recv_ts <= grid[n])]
        tau = (market.close_ts - grid[n]) / 1000
        if len(seen):
            ref[n] = ((w - tau) * seen.value.mean() + tau * ref[n]) / w
    return ref, age, line


def binance_leg_reference(grid, market, feeds):
    """Spot, its age and the line for the hourly leg (Binance 1h candle open vs close)."""
    spot = asof(grid, feeds["candles_live"], ["open_ts", "close"])
    ref, age = spot.close.values, (spot.ts - spot.open_ts - 1000).values
    line = np.full(len(grid), np.nan)
    if market.open_ts in feeds["candles_by_open"].index:
        row = feeds["candles_by_open"].loc[market.open_ts]
        if row.trades > 0:
            line[grid >= row.recv_ts] = row.open
    return ref, age, line


def event_table(legs, feeds, nu_by_horizon):
    """One row per second of an event: quotes, votes and the signal.

    legs: rows of the markets table that share one close_ts. The grid starts when the second leg
    opens (from then on there are 2+ live legs) and ends one second before expiry.
    """
    expiry = int(legs.close_ts.iloc[0])
    start = int(sorted(legs.open_ts)[-2])
    grid = np.arange(start, expiry, 1000, dtype="int64")
    tokens = list(legs.token_up)
    books = load("books", start - 300_000, expiry, filters=[("asset_id", "in", tokens)],
                 columns=["recv_ts", "src_ts", "asset_id", "bid_px_1", "ask_px_1"])
    ticks = load("clob_events", int(legs.open_ts.min()) - 600_000, expiry, columns=["recv_ts", "asset_id", "event_type", "price"],
                 filters=[("event_type", "==", "tick_size_change"), ("asset_id", "in", tokens)])
    table = pd.DataFrame({"expiry": expiry, "ts": grid, "tau": (expiry - grid) / 1000})
    table["sd"] = realised_sd(grid, feeds["candles_by_open"])
    # the last 5 minutes in 10-second steps: reacts when the market wakes up before the hourly figure does
    table["sd_fast"] = realised_sd(grid, feeds["candles_by_open"], returns=30, step_s=10)
    names = []
    for _, m in legs.sort_values("open_ts", ascending=False).iterrows():
        name, w = m.length, AVG_WINDOW_S[m.length]
        names.append(name)
        quotes = asof(grid, books[books.asset_id == m.token_up], ["src_ts", "bid_px_1", "ask_px_1"])
        live = grid >= m.open_ts
        if w:
            ref, age, line = twap_leg_reference(grid, m, feeds, w)
        else:
            ref, age, line = binance_leg_reference(grid, m, feeds)
        tick = np.full(len(grid), m.tick_size)
        for _, change in ticks[ticks.asset_id == m.token_up].sort_values("recv_ts").iterrows():
            tick[grid >= change.recv_ts] = change.price
        table[f"bid_{name}"] = quotes.bid_px_1.where(live).values
        table[f"ask_{name}"] = quotes.ask_px_1.where(live).values
        table[f"quote_age_{name}"] = (quotes.ts - quotes.src_ts).where(live).values
        table[f"spot_{name}"] = np.where(age <= MAX_FEED_AGE_MS, ref, np.nan)
        table[f"feed_age_{name}"] = age
        table[f"line_{name}"] = line
        table[f"tick_{name}"] = tick

    rows = []
    for r in table.itertuples(index=False):
        nu = nu_for(r.tau, nu_by_horizon)
        leg_inputs = []
        for n in names:
            spot, age_s = getattr(r, f"spot_{n}"), max(getattr(r, f"feed_age_{n}"), 0) / 1000
            # our spot is age_s seconds old; in that time the price moves by about spot * sd * sqrt(age_s) dollars
            stale = 0.0
            if STALENESS_ERR:
                sd_now = np.nanmax([r.sd, r.sd_fast]) if r.sd > 0 or r.sd_fast > 0 else np.nan
                stale = spot * sd_now * math.sqrt(age_s)
                if not stale >= 0:                  # no volatility figure: the error bar is unknown, so no vote
                    spot = np.nan
            leg_inputs.append({"name": n, "spot": spot, "line": getattr(r, f"line_{n}"),
                               "bid": getattr(r, f"bid_{n}"), "ask": getattr(r, f"ask_{n}"), "tick": getattr(r, f"tick_{n}"),
                               "line_err": math.hypot(LINE_ERR[n], stale), "tau": r.tau, "w": AVG_WINDOW_S[n]})
        s = signal(leg_inputs, r.sd, nu)
        row = {"nu": nu, "kind": s["kind"], "n_votes": s["n_votes"], "outlier": s["outlier"], "consensus": s["consensus"],
               "anchor": s["anchor"], "lo": s["lo"], "hi": s["hi"], "model": s["model"], "market": s["market"],
               "side": s["side"], "edge": s["edge"],
               "z_max": max((abs(z) for z in s["z"].values()), default=np.nan)}
        for vote in s["votes"]:
            row[f"x_{vote['name']}"], row[f"v_{vote['name']}"], row[f"sv_{vote['name']}"] = vote["x"], vote["v"], vote["sv"]
        # the same second under stricter limits; only worth recomputing when the disagreement is that large
        for limit in SCAN_LIMITS:
            strict = signal(leg_inputs, r.sd, nu, limit) if row["z_max"] > limit else {"kind": "none"}
            for col in SIGNAL_COLUMNS:
                row[f"{col}{limit_suffix(limit)}"] = strict.get(col) if col not in ("edge", "model") else strict.get(col, np.nan)
        rows.append(row)
    result = pd.concat([table, pd.DataFrame(rows)], axis=1)
    for n in names:                                   # make sure the columns exist even if a leg never voted
        for col in (f"x_{n}", f"v_{n}", f"sv_{n}"):
            if col not in result:
                result[col] = np.nan
    return result


def limit_suffix(limit):
    """Column suffix for a scanned limit: 3.5 -> '_z35'. The baseline Z_LIMIT has no suffix."""
    return "" if limit == Z_LIMIT else f"_z{round(limit * 10)}"


def list_events(start_ms, end_ms):
    """{expiry: legs} for expiries in [start_ms, end_ms) shared by 2 or more recorded markets."""
    markets = load("markets", start_ms - 2 * 86_400_000, end_ms).drop_duplicates("slug")
    markets = markets[(markets.close_ts >= start_ms) & (markets.close_ts < end_ms)]
    return {int(expiry): legs for expiry, legs in markets.groupby("close_ts") if len(legs) >= 2}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    expiry = int(pd.Timestamp(sys.argv[1], tz="UTC").timestamp() * 1000)
    events = list_events(expiry, expiry + 1)
    if expiry not in events:
        sys.exit(f"No recorded event with 2+ legs expires at {fmt_ts(expiry)} UTC")
    legs = events[expiry]
    feeds = load_feeds(int(legs.open_ts.min()) - 4_000_000, expiry + 5_000)
    table = event_table(legs, feeds, load_nu())
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 2000)
    pd.set_option("display.max_columns", 60)
    print(f"Expiry {fmt_ts(expiry)} UTC, legs {sorted(legs.length)}; votes shown as width in % over the time left\n")
    show = pd.DataFrame({"utc": table.ts.map(lambda t: fmt_ts(t)[11:]), "tau": table.tau.astype(int)})
    for n in sorted(legs.length, key=["5m", "15m", "1h"].index):
        show[f"bid_{n}"], show[f"ask_{n}"] = table[f"bid_{n}"], table[f"ask_{n}"]
        show[f"vote_{n}"] = (table[f"v_{n}"] * np.sqrt(table.tau) * 100).round(4)
    show["anchor"] = (table.anchor * np.sqrt(table.tau) * 100).round(4)
    show["z_max"] = table.z_max.round(1)
    for col in ("kind", "outlier", "lo", "hi", "side"):
        show[col] = table[col]
    show["model_c"], show["market_c"], show["edge_c"] = (table.model * 100).round(1), (table.market * 100).round(1), (table.edge * 100).round(1)
    step = 1 if len(sys.argv) > 2 and sys.argv[2] == "all" else 10
    print(show[(show.tau % step == 0) | (table.kind != "none")].to_string(index=False))
    print(f"\nSeconds: {len(table)}, with 2+ votes: {(table.n_votes >= 2).sum()}, "
          f"signals: {table.kind.value_counts().to_dict()}")


if __name__ == "__main__":
    main()
