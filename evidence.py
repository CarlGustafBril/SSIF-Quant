"""Is the signal valid? Tests that do not depend on thresholds or trade rules.

    python evidence.py summary       # the range trade on every dataset, pooled, and the sample still needed
    python evidence.py july          # the teammate's July tick recording (july.py), 07-11 to 07-18
    python evidence.py holdout       # the second delivery of tick extracts (07-18 21:00 to 07-22), see HISTORY.md
    python evidence.py polls         # the July 3-second polls on the dates the tick recording does not cover
    python evidence.py november      # our own recording
    python evidence.py now           # today's rule from the public trade tape (tape.py), 10-13 to 11-11
    python evidence.py autumn        # the same for 08-20 to 10-12
    python evidence.py july --variants     # also the rule variants, on each half of the data

The idea being tested: when two contracts that expire together imply different widths, one of them
is mispriced. If so, the other contract's width must say something the contract's own price does
not. For a contract A priced p, let m be the model's probability of Up at contract B's width.
The "gap" is m - p: how far B says A is mispriced. Two questions, one regression each:

  1. Outcome.     Does A finish Up more often than its price says when the gap is positive?
                  (Up - p) = beta * gap.  beta = 1: B's width was right. beta = 0: A's price was.
  2. Convergence. Does A's price move toward m over the next 30 or 60 seconds?
                  (p later - p now) = beta * gap.  beta = 1: fully closed. beta = 0: no move.

Both are run on every second where both contracts vote, and separately on signal seconds only
(disagreement beyond the limit). Seconds of one expiry are not independent, so standard errors
are clustered by expiry. The same is done with the realised-volatility width in B's place.

The data is split in two by time. Anything chosen by looking at the first half ("design") counts
only if it shows up again in the second ("confirm").
"""
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
from scipy import stats

import backtest
import bars
import july
import tape
import variants
import votes
from recorder import config
from recorder.reader import fmt_ts, load
from recorder.store import now_ms

LEGS = ("5m", "15m", "1h")
HORIZONS = (30, 60)
REPORTS = os.path.join(config.DATA_DIR, "reports")
pd.set_option("display.width", 220)
pd.set_option("display.max_rows", 500)


# ============================================================================ data
TIMING = {"july": (3, 1), "holdout": (3, 1), "november": (3, 1), "now": (3, 1), "autumn": (3, 1), "polls": (6, 4)}
TAPE_FILL_OPTIMISM = 0.009     # dollars per trade by which trade-built quotes flatter fills (tape.py validate, July)     # seconds a signal must hold, then seconds to the fill


class timing:
    """Persistence and fill delay for a dataset. The polls arrive about every 3 s, so a signal there
    must span two polls (6 s) and is filled at a later poll (4 s on). Set before any result was seen."""
    def __init__(self, name):
        self.new = TIMING[name]

    def __enter__(self):
        self.saved = backtest.PERSIST_S, backtest.DELAY_S
        backtest.PERSIST_S, backtest.DELAY_S = self.new

    def __exit__(self, *exc):
        backtest.PERSIST_S, backtest.DELAY_S = self.saved


def dataset(name):
    """(signal table, outcomes, averaging windows) for 'july', 'polls' or 'november'."""
    if name == "july":
        return july.load().sort_values(["expiry", "ts"]).reset_index(drop=True), july.outcomes(), july.WINDOWS
    if name in ("now", "autumn"):
        return tape.load_now(name).sort_values(["expiry", "ts"]).reset_index(drop=True), tape.now_outcomes(), dict(votes.AVG_WINDOW_S)
    if name == "holdout":
        return july.load("holdout").sort_values(["expiry", "ts"]).reset_index(drop=True), july.outcomes(), july.WINDOWS
    if name == "polls":                       # only expiries no tick extract covers: an independent sample
        covered = set(july.load().expiry.unique()) | set(july.load("holdout").expiry.unique())
        polls = july.load_rest()
        polls = polls[~polls.expiry.isin(covered)]
        return polls.sort_values(["expiry", "ts"]).reset_index(drop=True), july.outcomes(), july.WINDOWS
    start = int(load("health", 0, now_ms(), columns=["recv_ts"]).recv_ts.min())
    signals, _ = backtest.load_days(start, now_ms() - 60_000)
    res = load("resolutions", start, now_ms()).drop_duplicates("slug", keep="last")
    res = res[res.outcome.isin(["Up", "Down"])]
    outcomes = dict(zip(zip(res.close_ts, res.length), (res.outcome == "Up").astype(float)))
    return signals.sort_values(["expiry", "ts"]).reset_index(drop=True), outcomes, dict(votes.AVG_WINDOW_S)


def halves(signals):
    expiries = np.sort(signals.expiry.unique())
    cut = expiries[len(expiries) // 2]
    return {"design": signals[signals.expiry < cut], "confirm": signals[signals.expiry >= cut]}


# ============================================================================ the gap table
def gap_rows(signals, outcomes, windows):
    """One row per second, contract A and source of the other width (contract B or realised vol)."""
    s = signals
    te = {n: np.array([votes.tau_eff(t, windows[n]) for t in s.tau]) for n in LEGS if f"v_{n}" in s}
    later = {h: s[["expiry", "ts"]].assign(ts=s.ts - h * 1000) for h in HORIZONS}
    parts = []
    for a in te:
        mid = ((s[f"bid_{a}"] + s[f"ask_{a}"]) / 2).values
        up = np.array([outcomes.get((e, a), np.nan) for e in s.expiry])
        moves = {}
        for h in HORIZONS:                     # mid of A h seconds later, same expiry
            future = pd.DataFrame({"expiry": s.expiry, "ts": s.ts - h * 1000, f"mid_{h}": mid})
            merged = s[["expiry", "ts"]].merge(future, on=["expiry", "ts"], how="left")
            moves[h] = merged[f"mid_{h}"].values - mid
        for b in [n for n in te if n != a] + ["vol"]:
            width = s["anchor"].values if b == "vol" else s[f"v_{b}"].values
            ok = s[f"v_{a}"].notna().values & np.isfinite(width) & (width > 0)
            if not ok.any():
                continue
            model = stats.t.sf(s[f"x_{a}"].values[ok] / (width[ok] * np.sqrt(te[a][ok])), s.nu.values[ok])
            part = pd.DataFrame({"expiry": s.expiry.values[ok], "ts": s.ts.values[ok], "tau": s.tau.values[ok],
                                 "a": a, "b": b, "p": mid[ok], "gap": model - mid[ok], "up": up[ok],
                                 "half_spread": ((s[f"ask_{a}"] - s[f"bid_{a}"]) / 2).values[ok]})
            if b == "vol":
                part["z"] = np.nan
            else:
                part["z"] = ((s[f"v_{a}"] - s[f"v_{b}"]).abs() / np.hypot(s[f"sv_{a}"], s[f"sv_{b}"])).values[ok]
            for h in HORIZONS:
                part[f"move_{h}"] = moves[h][ok]
            parts.append(part)
    return pd.concat(parts, ignore_index=True)


def slope(d, y, x="gap"):
    """Slope of y on x through the origin, with a standard error clustered by expiry."""
    d = d[[x, y, "expiry"]].dropna()
    if len(d) < 50 or d.expiry.nunique() < 10:
        return {"n": len(d), "events": d.expiry.nunique(), "beta": np.nan, "se": np.nan}
    sxx = (d[x] ** 2).sum()
    beta = (d[x] * d[y]).sum() / sxx
    score = (d[x] * (d[y] - beta * d[x])).groupby(d.expiry).sum()
    return {"n": len(d), "events": d.expiry.nunique(), "beta": beta, "se": np.sqrt((score ** 2).sum()) / sxx}


def gap_tests(gaps, title, z_limit=votes.Z_LIMIT, min_tau=0):
    print(f"\n--- {title} ---")
    rows = []
    for (a, b), d in gaps.groupby(["a", "b"]):
        d = d[d.tau > min_tau]
        for label, part in (("all seconds", d), (f"signal seconds (beyond {z_limit} bars)", d[d.z > z_limit])):
            if b == "vol" and label != "all seconds":
                continue
            row = {"contract": a, "other width from": b, "seconds": label, "mean |gap| c": part.gap.abs().mean() * 100}
            o = slope(part, "up_minus_p")
            row.update(n=o["n"], events=o["events"], outcome_beta=o["beta"], outcome_se=o["se"])
            for h in HORIZONS:
                m = slope(part, f"move_{h}")
                row[f"move{h}_beta"], row[f"move{h}_se"] = m["beta"], m["se"]
            rows.append(row)
    table = pd.DataFrame(rows)
    print(table.round(3).to_string(index=False))
    return table


def decile_table(gaps, a, b):
    """Outcome by size of the gap: what the price said, what the other width said, what happened."""
    d = gaps[(gaps.a == a) & (gaps.b == b)].dropna(subset=["up"])
    if len(d) < 500:
        return None
    d = d.assign(bucket=pd.qcut(d.gap, 10, duplicates="drop"))
    g = d.groupby("bucket", observed=True)
    out = pd.DataFrame({"seconds": g.size(), "events": g.expiry.nunique(), "gap_c": g.gap.mean() * 100,
                        "price": g.p.mean(), "other_width_says": (g.p.mean() + g.gap.mean()),
                        "finished_up": g.up.mean()})
    out["actual_minus_price_c"] = (out.finished_up - out.price) * 100
    return out.reset_index(drop=True)


def leg_trades(gaps, a, b, min_gap, z_limit=votes.Z_LIMIT, persist=3, delay=1):
    """Trade contract A alone when B says it is mispriced by min_gap after the spread. One trade per expiry.

    Entered `delay` seconds after the gap has held `persist` seconds, at that second's bid or ask
    (approximated by the mid plus half the spread seen then), held to expiry.
    """
    d = gaps[(gaps.a == a) & (gaps.b == b)].sort_values(["expiry", "ts"]).reset_index(drop=True)
    edge = d.gap.abs() - d.half_spread
    ok = ((d.z > z_limit) & (edge >= min_gap)).values
    side = np.sign(d.gap.values)
    run = np.zeros(len(d), dtype=int)
    for i in range(len(d)):
        if ok[i]:
            same = i > 0 and ok[i - 1] and d.expiry[i] == d.expiry[i - 1] and d.ts[i] - d.ts[i - 1] == 1000 and side[i] == side[i - 1]
            run[i] = run[i - 1] + 1 if same else 1
    hits = d[run == persist].assign(side=side[run == persist])
    entry = hits.assign(ts=hits.ts + delay * 1000)[["expiry", "ts", "side"]].merge(
        d[["expiry", "ts", "p", "half_spread", "gap", "up"]], on=["expiry", "ts"])
    entry = entry[(np.sign(entry.gap) == entry.side) & (entry.gap.abs() - entry.half_spread >= min_gap)]
    entry = entry.sort_values("ts").drop_duplicates("expiry").dropna(subset=["up"])
    pnl = entry.side * (entry.up - entry.p) - entry.half_spread
    return entry.assign(pnl=pnl)


# ============================================================================ variants on each half
def _variant(args):
    name, signals, outcomes, windows, data = args
    spec = variants.VARIANTS[name]
    cfg = {**variants.DEFAULTS, **{k: v for k, v in spec.items() if k != "desc"}}
    if cfg["bars"] == "measured":
        cfg["spot_kinds"], cfg["quote_extra_s"] = variants.SPOT_KINDS[data]
        if data == "november":                  # only our own recording has the feed to move forward
            signals = bars.nowcast_table(signals)
    saved = dict(votes.AVG_WINDOW_S)
    votes.AVG_WINDOW_S.update(windows)
    try:
        table = variants.redecide(signals, cfg)
    finally:
        votes.AVG_WINDOW_S.update(saved)
    rows = []
    with timing(data):
        for period, part in (("all", table), *halves(table).items()):
            rows.append({"data": data, "variant": name, "period": period, **variants.evaluate(part, outcomes, cfg)})
        trades, _ = backtest.simulate(table, outcomes, cfg["threshold"])      # kept, so that samples can be pooled later
    trades.assign(pnl=trades.pnl.astype(float)).to_parquet(variant_trades_path(data, name))
    return rows


def variant_trades_path(data, variant):
    return os.path.join(config.DATA_DIR, "analysis", f"trades_{data}_{variant}.parquet")


def variant_pooled(variant, title):
    """One rule variant across the samples that share no expiry: July tick (both deliveries) and the
    public tape under today's rule. Uses the trades saved by the last `--variants` run of each."""
    backtest.section(title)
    rows, parts = [], []
    for data, label in (("july", "July tick, first delivery (old rule)"), ("holdout", "July tick, second delivery (old rule)"),
                        ("now", "Public trade tape, 10-13 to 11-11 (today's rule)"),
                        ("autumn", "Public trade tape, 08-20 to 10-12 (today's rule)")):
        path = variant_trades_path(data, variant)
        if not os.path.exists(path):
            print(f"  no saved trades for {data}: run  python evidence.py {data} --variants")
            continue
        t = pd.read_parquet(path).dropna(subset=["pnl"]).assign(data=data)
        if data in ("now", "autumn"):
            t["pnl_net"] -= TAPE_FILL_OPTIMISM
        parts.append(t)
        for column, kind in (("pnl", "before fees"), ("pnl_net", "after fees")):
            n, mean, se = interval(t[column] * 100)
            rows.append({"data": label, "trades": n, "P&L": kind, "hit": (t[column] > 0).mean(), "mean c": mean, "se": se,
                         "95% low": mean - 1.96 * se, "95% high": mean + 1.96 * se})
    if not parts:
        return
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    both = pd.concat(parts)
    print(f"\nPooled: {len(both)} trades")
    for column, kind in (("pnl", "before fees"), ("pnl_net", "after fees ")):
        n, mean, se = interval(both[column] * 100)
        print(f"  {kind}: {mean:+.1f} c per trade, se {se:.1f}, 95% interval {mean - 1.96 * se:+.1f} to {mean + 1.96 * se:+.1f}, t = {mean / se:.1f}")


def variant_table(signals, outcomes, windows, data, names=None):
    names = names or list(variants.VARIANTS)
    with ProcessPoolExecutor(min(len(names), 8)) as pool:
        rows = [r for part in pool.map(_variant, [(n, signals, outcomes, windows, data) for n in names]) for r in part]
    return pd.DataFrame(rows).drop(columns=["trades_detail"])


# ============================================================================ the range trade, pooled
def interval(pnl_cents):
    n = len(pnl_cents)
    mean = pnl_cents.mean() if n else np.nan
    se = pnl_cents.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    return n, mean, se


def range_trades(name, threshold=backtest.DEFAULT_THRESHOLD):
    """The frozen rules' trades on one dataset (one per expiry, held to expiry), with fees."""
    signals, outcomes, _ = dataset(name)
    with timing(name):
        trades, _ = backtest.simulate(signals, outcomes, threshold)
    trades = trades.dropna(subset=["pnl"]).assign(data=name)
    if name in ("now", "autumn"):              # "after fees" on the tape also takes off what the tape flatters fills by
        trades["pnl_net"] -= TAPE_FILL_OPTIMISM
    return trades, signals.expiry.nunique()


DATASETS = {"july": "July tick recording, 07-11 to 07-18 (old settlement rule)",
            "holdout": "July tick recording, 07-18 to 07-22, expectations fixed first",
            "polls": "July 3-second polls, expiries without tick data (old rule, coarser)",
            "autumn": "Public trade tape, 08-20 to 10-12 (today's rule), test fixed first",
            "now": "Public trade tape, 10-13 to 11-11 (today's rule), test fixed first",
            "november": "Our recording, from 11-11 (today's rule)"}


def size_at_fill(trades):
    """Shares on offer at the touch of both legs when each July tick trade was filled (the smaller of the two)."""
    files = {july.expiry_of(p) * 1000: p for part in ("tick", "holdout") for p in july.event_files(part)}
    sizes = []
    for t in trades.itertuples():
        rows = pd.read_parquet(files[t.expiry], columns=["ts_ms", "rowtype", "leg", "bid_sz", "ask_sz", "ex_ms"])
        rows["recv_ts"] = (rows.ts_ms - july.clock_offset_ms(rows)).round().astype("int64")
        books = rows[rows.rowtype == "book"].sort_values("recv_ts", kind="stable")
        fill = np.array([t.ts + backtest.DELAY_S * 1000], dtype="int64")
        lo = votes.asof(fill, books[books.leg == t.lo], ["bid_sz", "ask_sz"]).iloc[0]
        hi = votes.asof(fill, books[books.leg == t.hi], ["bid_sz", "ask_sz"]).iloc[0]
        sizes.append(min(lo.ask_sz, hi.bid_sz) if t.side == "buy" else min(lo.bid_sz, hi.ask_sz))
    return pd.Series(sizes, index=trades.index)


def capacity(trades):
    backtest.section("How much can be traded? Size at the best price when each July tick trade was filled")
    shares = size_at_fill(trades).fillna(0)
    q = shares.quantile([0.25, 0.5, 0.75])
    print(f"Shares available on both legs at once: median {q[0.5]:.0f}, middle half {q[0.25]:.0f} to {q[0.75]:.0f} "
          f"(a share pays $1 at most; the range costs {trades.price.mean() * 100:.0f} cents on average).")
    days = (trades.expiry.max() - trades.expiry.min()) / 86_400_000
    for cap in (50, 200, 1000):
        taken = shares.clip(upper=cap)
        print(f"  taking at most {cap:>4} shares per trade: ${(taken * trades.pnl_net).sum():,.0f} after fees over {days:.0f} days "
              f"({len(trades)} trades, {taken.mean():.0f} shares per trade on average)")
    print("This is what the top of the book would have filled. It ignores that our own order moves the price,")
    print("and the dollar totals carry the same uncertainty as the cents per trade above.")


def summary():
    backtest.section("The range trade under the frozen rules, on every dataset")
    print("One trade per expiry at the first signal that lasts, 4.5 cents minimum edge, held to expiry.")
    print(f"Cents per share. Fee: the exchange's taker fee, {backtest.FEE_RATE} x p x (1 - p) per share, on both legs.\n")
    rows, parts = [], {}
    for name, label in DATASETS.items():
        trades, events = range_trades(name)
        for column, kind in (("pnl", "before fees"), ("pnl_net", "after fees")):
            n, mean, se = interval(trades[column] * 100)
            rows.append({"data": label, "events": events, "trades": n, "P&L": kind, "hit": (trades[column] > 0).mean() if n else np.nan,
                         "mean c": mean, "se": se, "95% low": mean - 1.96 * se, "95% high": mean + 1.96 * se})
        parts[name] = trades
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    print(f"On the trade tape, 'after fees' also takes off {TAPE_FILL_OPTIMISM * 100:.1f} c, by which trade-built quotes flatter fills.")

    def pool(names, title):
        both = pd.concat([parts[n] for n in names]).drop_duplicates("expiry", keep="last")     # our recording wins over the tape
        print(f"\n{title}: {len(both)} trades")
        for column, kind in (("pnl", "before fees"), ("pnl_net", "after fees ")):
            n, mean, se = interval(both[column] * 100)
            print(f"  {kind}: {mean:+.1f} c per trade, se {se:.1f}, 95% interval {mean - 1.96 * se:+.1f} to {mean + 1.96 * se:+.1f}, "
                  f"t = {mean / se:.1f}")
        return both
    tick = pool(["july", "holdout"], "July tick recording, both deliveries (old rule)")
    july_all = pool(["july", "holdout", "polls"], "All of July (old rule)")
    today = pool(["autumn", "now", "november"], "Today's rule: public tape and our recording")
    everything = pool(list(DATASETS), "Everything")
    sd = everything.pnl_net.std(ddof=1) * 100
    print(f"\nOne trade's P&L has a standard deviation of {sd:.0f} cents. To tell a true edge after fees from zero at")
    print("two standard errors takes (2 x sd / edge)^2 trades:")
    for edge in (3, 5, 8):
        print(f"  true edge {edge} c: {int(np.ceil((2 * sd / edge) ** 2)):,} trades")
    print(f"Trades per day: July tick recording {len(parts['july']) / 7.1:.0f}, public tape under today's rule "
          f"{(len(parts['now']) + len(parts['autumn'])) / 84:.0f}.")

    capacity(tick)

    backtest.section("Does a bigger edge at entry mean a bigger profit? (July pooled, 2 cents minimum edge)")
    low = pd.concat([range_trades(n, 0.02)[0] for n in ("july", "holdout", "polls")])
    low["edge"] = pd.cut(low.edge_at_entry * 100, [2, 4.5, 8, 12, 100], right=False)
    g = low.assign(c=low.pnl * 100, n_c=low.pnl_net * 100, e=low.edge_at_entry * 100).groupby("edge", observed=True)
    print(pd.DataFrame({"trades": g.size(), "edge at entry c": g.e.mean(), "P&L c": g.c.mean(),
                        "se": g.c.std() / np.sqrt(g.size()), "after fees c": g.n_c.mean()}).round(1).to_string())
    d = low.assign(c=low.pnl * 100, e=low.edge_at_entry * 100)
    slope_ = (d.e * d.c).sum() / (d.e ** 2).sum()
    resid = d.c - slope_ * d.e
    print(f"P&L per cent of edge (line through zero): {slope_:.2f}, se {np.sqrt((d.e ** 2 * resid ** 2).sum()) / (d.e ** 2).sum():.2f}. "
          "1.0 would mean the model's edge is earned in full.")

    for title, j_all in (("July, old rule", july_all), ("today's rule", today)):
        by_group(title, j_all)
    variant_pooled("same_feed", "The candidate rule: 5m and 15m only (same_feed), on the three samples it was judged on")
    variant_pooled("baseline", "For comparison, the rule of record on the same three samples")


def by_group(title, july_all):
    backtest.section(f"By pair of contracts, side and dataset ({title}, frozen rules)")
    j = july_all.assign(pair=np.where(july_all.lo.isin(["1h"]) | july_all.hi.isin(["1h"]), "one leg is the 1h", "5m and 15m"),
                        c=july_all.pnl * 100, n_c=july_all.pnl_net * 100)
    for by in ("pair", "side", "kind", "data"):
        g = j.groupby(by)
        print(pd.DataFrame({"trades": g.size(), "hit": g.c.apply(lambda x: (x > 0).mean()), "P&L c": g.c.mean(),
                            "se": g.c.std() / np.sqrt(g.size()), "after fees c": g.n_c.mean()}).round(2).to_string() + "\n")


# ============================================================================ report
def report(name, with_variants):
    signals, outcomes, windows = dataset(name)
    print(f"Evidence report, {name} data, run {fmt_ts(now_ms())} UTC")
    print(f"Events: {signals.expiry.nunique()} from {fmt_ts(signals.expiry.min())} to {fmt_ts(signals.expiry.max())} UTC; "
          f"seconds with 2+ votes: {(signals.n_votes >= 2).sum():,}; with 3: {(signals.n_votes >= 3).sum():,}")
    backtest.error_bar_check(signals)

    backtest.section("A. Does the other contract's width predict the outcome or the next price move?")
    print("beta = 1: the other width was right and the price moves to it. beta = 0: it adds nothing.")
    print("se: standard error clustered by expiry. |beta| under 2 se is not distinguishable from 0.")
    gaps = gap_rows(signals, outcomes, windows)
    gaps["up_minus_p"] = gaps.up - gaps.p
    cut = np.sort(signals.expiry.unique())[signals.expiry.nunique() // 2]
    tables = {}
    for label, part in (("all data", gaps), ("design half", gaps[gaps.expiry < cut]), ("confirm half", gaps[gaps.expiry >= cut])):
        tables[label] = gap_tests(part, label)

    backtest.section("B. Outcome by size of the gap (all data)")
    print("Rows are tenths of seconds ordered by gap. If the other width is right, 'finished_up' follows")
    print("'other_width_says'. If the contract's own price is right, it follows 'price'.")
    for a, b in (("5m", "15m"), ("15m", "5m"), ("15m", "1h"), ("1h", "15m"), ("5m", "vol"), ("15m", "vol"), ("1h", "vol")):
        t = decile_table(gaps, a, b)
        if t is not None:
            print(f"\n{a}, other width from {b}:")
            print(t.round(3).to_string(index=False))

    backtest.section("C. Trading one contract alone on the other's width (held to expiry, cents per share)")
    print("Signal beyond the limit, gap after half the spread at least the threshold, held and filled as the range trade. Before fees.")
    rows = []
    persist, delay = TIMING[name]
    for a, b in (("5m", "15m"), ("15m", "5m"), ("15m", "1h"), ("1h", "15m")):
        for min_gap in (0.02, 0.045, 0.08):
            for label, part in (("design", gaps[gaps.expiry < cut]), ("confirm", gaps[gaps.expiry >= cut])):
                t = leg_trades(part, a, b, min_gap, persist=persist, delay=delay)
                s = backtest.summarise(t)
                rows.append({"trade": a, "width from": b, "min gap c": min_gap * 100, "half": label, "trades": s["trades"],
                             "hit": s["hit_rate"], "pnl c": s["pnl_cents"], "ci low": s["ci_low"], "ci high": s["ci_high"]})
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    if with_variants:
        backtest.section("D. Rule variants (range trade as specified), all data and each half")
        v = variant_table(signals, outcomes, windows, name)
        print(v.to_string(index=False))
        os.makedirs(variants.RESULTS_DIR, exist_ok=True)
        path = os.path.join(variants.RESULTS_DIR, f"variants_{name}.csv")
        v.assign(run_utc=fmt_ts(now_ms()), code_version=variants.git_version()).to_csv(path, index=False)
        print(f"\nSaved to {path}")
    return gaps, tables


def only_variants(data, names):
    """Chosen variants on one dataset, without the rest of the report:  python evidence.py variants autumn baseline measured_bars"""
    signals, outcomes, windows = dataset(data)
    v = variant_table(signals, outcomes, windows, data, names)
    print(v.to_string(index=False))
    path = os.path.join(variants.RESULTS_DIR, f"variants_{data}_{'_'.join(n for n in names if n != 'baseline')}.csv")
    v.assign(run_utc=fmt_ts(now_ms()), code_version=variants.git_version()).to_csv(path, index=False)
    print(f"\nSaved to {path}")


def tape_pooled(names):
    """Chosen variants on both public-tape periods together (today's rule), from the trades their runs saved:
    python evidence.py tape-pooled baseline measured_bars empirical"""
    rows = []
    for name in names:
        parts = [pd.read_parquet(variant_trades_path(data, name)).dropna(subset=["pnl"]) for data in ("autumn", "now")]
        t = pd.concat(parts)
        n, mean, se = interval(t.pnl * 100)
        _, net, net_se = interval((t.pnl_net - TAPE_FILL_OPTIMISM) * 100)
        rows.append({"variant": name, "trades": n, "hit": (t.pnl > 0).mean(), "before fees c": mean, "se": se, "t": mean / se,
                     "95% low": mean - 1.96 * se, "95% high": mean + 1.96 * se, "after fees and 0.9 c": net, "net se": net_se,
                     "net 95% low": net - 1.96 * net_se, "net 95% high": net + 1.96 * net_se})
    print(pd.DataFrame(rows).round(2).to_string(index=False))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    name = args[0] if args else "summary"
    if name == "variants":
        return only_variants(args[1], args[2:])
    if name == "tape-pooled":
        return tape_pooled(args[1:])
    if name not in ("july", "holdout", "polls", "november", "now", "autumn", "summary"):
        sys.exit(__doc__)
    import io
    buffer, stdout = io.StringIO(), sys.stdout

    class Tee:
        def write(self, text):
            stdout.write(text)
            buffer.write(text)

        def flush(self):
            stdout.flush()

    sys.stdout = Tee()
    try:
        if name == "summary":
            print(f"Evidence summary, run {fmt_ts(now_ms())} UTC, code {variants.git_version()}")
            summary()
        else:
            report(name, "--variants" in sys.argv)
    finally:
        sys.stdout = stdout
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"evidence_{name}_{pd.Timestamp(now_ms(), unit='ms').strftime('%Y-%m-%d_%H%M')}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(buffer.getvalue())
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
