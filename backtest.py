"""Backtest of the range trade on the recorded data.

    python backtest.py                                    # everything recorded so far
    python backtest.py 2026-11-12 2026-11-20              # UTC days, end exclusive
    python backtest.py --brief-bars                       # error bars exactly as in the brief (no staleness term)

Steps:
  1. For every event (an expiry shared by 2+ legs) build the per-second signal table with votes.py.
     For every market also sample the model probability every 10 seconds (for the calibration).
     Both are cached per UTC day in <POLYDATA_DIR>/analysis/, because this is the slow part.
     The cache is rebuilt by itself whenever votes.py or nu.json changes.
  2. Split the events by time: the first 60% fit the edge threshold, the rest are reported.
  3. Report trades and P&L, whether prices move toward the model after a signal, false alarms in
     quiet periods, calibration (normal vs t), monotonicity.

Rules of the simulation: one position per expiry, fixed size of 1 share per leg, held to expiry.
A signal must hold for 3 seconds in a row (our spot price is about 2 seconds old, and a signal that
vanishes once the feed catches up was never a mispricing). It is then executed at the quotes of the
next second, and only if the edge is still at least the threshold there. No fees, no size limits.
Lines for 5m and 15m come from our recorded feed. Official values are used only for outcomes.
"""
import glob
import hashlib
import io
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

import svgplot
import votes
from recorder import config
from recorder.reader import fmt_ts, load
from recorder.store import now_ms

DAY = 86_400_000
OUT_DIR = os.path.join(config.DATA_DIR, "analysis")
TRAIN_SHARE = 0.6
THRESHOLDS = np.arange(0.01, 0.1001, 0.005)          # candidate minimum edges, dollars per share
DEFAULT_THRESHOLD = 0.045                            # used when the first part has too few trades to choose
MIN_TRAIN_TRADES = 30
DELAY_S = 1                                          # seconds between seeing a signal and trading on it
TARGET_SIGNAL_RATE = 0.01                            # the disagreement limit is chosen to signal on at most this share of seconds
PERSIST_S = 3                                        # a signal must hold this many seconds in a row before we act
SAMPLE_EVERY_S = 10                                  # calibration sampling step
LEGS = ("5m", "15m", "1h")
Z_LIMIT = votes.Z_LIMIT
SUMMARY = []                                         # one line per headline number, printed at the end
pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 500)


def section(title):
    print("\n" + "=" * 96 + f"\n{title}\n" + "=" * 96)


# ============================================================================ step 1: per-day tables
def calibration_rows(market, feeds, nu_by_horizon):
    """Every 10 seconds of one market's window: moneyness, time left and realised vol (no prices needed)."""
    grid = np.arange(market.open_ts + SAMPLE_EVERY_S * 1000, market.close_ts, SAMPLE_EVERY_S * 1000, dtype="int64")
    w = votes.AVG_WINDOW_S[market.length]
    if w:
        spot, age, line = votes.twap_leg_reference(grid, market, feeds, w)
    else:
        spot, age, line = votes.binance_leg_reference(grid, market, feeds)
    tau = (market.close_ts - grid) / 1000
    rows = pd.DataFrame({"slug": market.slug, "length": market.length, "ts": grid, "tau": tau,
                         "x": np.log(line / spot), "te": [votes.tau_eff(t, w) for t in tau],
                         "sd": votes.realised_sd(grid, feeds["candles_by_open"]),
                         "nu": [votes.nu_for(t, nu_by_horizon) for t in tau]})
    return rows[(age <= votes.MAX_FEED_AGE_MS) & rows.x.notna() & rows.sd.notna()]


def build_day(day_start, nu_by_horizon):
    """Signal table and calibration samples for the markets expiring on one UTC day."""
    end = min(day_start + DAY, now_ms() - 60_000)
    feeds = votes.load_feeds(day_start - 2 * 3_600_000, end + 10_000)
    markets = load("markets", day_start - 2 * DAY, end).drop_duplicates("slug")
    markets = markets[(markets.close_ts >= day_start) & (markets.close_ts < end)]
    events = [votes.event_table(legs, feeds, nu_by_horizon)
              for _, legs in markets.groupby("close_ts") if len(legs) >= 2]
    calib = [calibration_rows(m, feeds, nu_by_horizon) for m in markets.itertuples(index=False)]
    signals = pd.concat(events, ignore_index=True) if events else pd.DataFrame()
    for col in signals.columns:
        if col.split("_z")[0] in ("kind", "outlier", "lo", "hi", "side"):
            signals[col] = signals[col].astype("string")
    return signals, pd.concat(calib, ignore_index=True) if calib else pd.DataFrame()


def cache_tag():
    """Changes whenever the vote code, nu or the error-bar setting changes, so stale caches are never reused."""
    h = hashlib.md5(str(votes.STALENESS_ERR).encode())
    for path in (votes.__file__, votes.NU_FILE):
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:8]


def load_days(start, end):
    nu_by_horizon = votes.load_nu()
    os.makedirs(OUT_DIR, exist_ok=True)
    all_signals, all_calib = [], []
    tag = cache_tag()
    for day_start in range(start // DAY * DAY, end, DAY):
        day = pd.Timestamp(day_start, unit="ms").strftime("%Y-%m-%d")
        paths = [os.path.join(OUT_DIR, f"{name}_{day}_{tag}.parquet") for name in ("signals", "calibration")]
        finished = day_start + DAY < now_ms() - 60_000                 # only finished days are cached
        if finished and all(os.path.exists(p) for p in paths):
            signals, calib = pd.read_parquet(paths[0]), pd.read_parquet(paths[1])
        else:
            print(f"building {day} ...", flush=True)
            signals, calib = build_day(day_start, nu_by_horizon)
            if finished and len(signals):
                for name in ("signals", "calibration"):            # drop caches made by older code
                    for old in glob.glob(os.path.join(OUT_DIR, f"{name}_{day}*.parquet")):
                        os.remove(old)
                signals.to_parquet(paths[0])
                calib.to_parquet(paths[1])
        all_signals.append(signals)
        all_calib.append(calib)
    all_signals, all_calib = [s for s in all_signals if len(s)], [c for c in all_calib if len(c)]
    if not all_signals or not all_calib:
        return pd.DataFrame(), pd.DataFrame()
    signals, calib = pd.concat(all_signals, ignore_index=True), pd.concat(all_calib, ignore_index=True)
    return signals[(signals.expiry >= start) & (signals.expiry < end)], calib[(calib.ts >= start) & (calib.ts < end)]


# ============================================================================ step 2: trades
def pick(table, prefix, names):
    """For each row, the value of column prefix_<name> where name is that row's entry in `names`."""
    out = np.full(len(table), np.nan)
    for leg in LEGS:
        col = f"{prefix}_{leg}"
        if col in table:
            chosen = (names == leg).fillna(False).values
            out[chosen] = table[col].values[chosen]
    return out


def run_length(table, ok):
    """For each row, how many seconds in a row (ending here) the same trade has been on with `ok` true."""
    t = table.sort_values(["expiry", "ts"])
    good = np.asarray(ok.reindex(t.index), dtype=bool)
    trade = (t.side.astype(object).fillna("") + t.lo.astype(object).fillna("") + t.hi.astype(object).fillna("")).values
    same = np.zeros(len(t), dtype=bool)                 # same trade as the second before
    same[1:] = (good[1:] & good[:-1] & (t.expiry.values[1:] == t.expiry.values[:-1])
                & (np.diff(t.ts.values) == 1000) & (trade[1:] == trade[:-1]))
    length = np.zeros(len(t), dtype=int)
    for n in range(len(t)):
        if good[n]:
            length[n] = length[n - 1] + 1 if same[n] else 1
    return pd.Series(length, index=t.index).reindex(table.index)


def simulate(signals, outcomes, threshold):
    """One trade per expiry: the first time the edge has been at least the threshold for PERSIST_S seconds.

    outcomes: {(expiry, length): 1.0 if Up else 0.0}. Returns one row per trade with P&L in dollars per share.
    """
    quote_cols = [c for c in signals.columns if c.startswith(("bid_", "ask_"))]
    ok = signals.side.notna() & (signals.edge >= threshold)
    hits = signals.loc[run_length(signals, ok) >= PERSIST_S,
                       ["expiry", "ts", "tau", "kind", "outlier", "lo", "hi", "side", "edge", "model"]]
    hits = hits.assign(entry_ts=hits.ts + DELAY_S * 1000)
    later = signals[["expiry", "ts"] + quote_cols].rename(columns={"ts": "entry_ts"})
    entry = hits.merge(later, on=["expiry", "entry_ts"])
    lo_bid, lo_ask = pick(entry, "bid", entry.lo), pick(entry, "ask", entry.lo)
    hi_bid, hi_ask = pick(entry, "bid", entry.hi), pick(entry, "ask", entry.hi)
    buy = (entry.side == "buy").values
    entry["price"] = np.where(buy, lo_ask - hi_bid, lo_bid - hi_ask)        # paid for the range, or received
    entry["edge_at_entry"] = np.where(buy, entry.model - entry.price, entry.price - entry.model)
    seen = hits.expiry.nunique()
    entry = entry[entry.edge_at_entry >= threshold].sort_values("ts").drop_duplicates("expiry")
    buy = (entry.side == "buy").values
    up_lo = np.array([outcomes.get((e, l), np.nan) for e, l in zip(entry.expiry, entry.lo)])
    up_hi = np.array([outcomes.get((e, l), np.nan) for e, l in zip(entry.expiry, entry.hi)])
    entry["payoff"] = up_lo - up_hi                                         # what the range pays
    entry["pnl"] = np.where(buy, entry.payoff - entry.price, entry.price - entry.payoff)
    cols = ["expiry", "ts", "tau", "kind", "outlier", "lo", "hi", "side", "edge", "edge_at_entry", "price", "payoff", "pnl"]
    return entry[cols], seen - len(entry)


def summarise(trades):
    """Count, hit rate, mean P&L in cents and its 95% confidence interval."""
    done = trades.dropna(subset=["pnl"])
    n = len(done)
    if n == 0:
        return {"trades": 0, "hit_rate": np.nan, "pnl_cents": np.nan, "ci_low": np.nan, "ci_high": np.nan, "total_cents": 0.0}
    pnl = done.pnl * 100
    half = stats.t.ppf(0.975, n - 1) * pnl.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    return {"trades": n, "hit_rate": (pnl > 0).mean(), "pnl_cents": pnl.mean(),
            "ci_low": pnl.mean() - half, "ci_high": pnl.mean() + half, "total_cents": pnl.sum()}


def trading_report(signals, outcomes):
    section("1. Events, signals and trades")
    expiries = np.sort(signals.expiry.unique())
    cut = expiries[int(len(expiries) * TRAIN_SHARE)] if len(expiries) > 1 else expiries[-1] + 1
    train, test = signals[signals.expiry < cut], signals[signals.expiry >= cut]
    print(f"Events: {len(expiries)} from {fmt_ts(expiries[0])} to {fmt_ts(expiries[-1])} UTC")
    print(f"  first part (fit the threshold): {train.expiry.nunique()} events up to {fmt_ts(cut)}")
    print(f"  second part (reported):         {test.expiry.nunique()} events")

    def counts(part):
        legs3 = part.groupby("expiry").n_votes.max()
        return {"events": part.expiry.nunique(), "seconds": len(part), "seconds_2+_votes": int((part.n_votes >= 2).sum()),
                "seconds_3_votes": int((part.n_votes >= 3).sum()), "events_ever_3_votes": int((legs3 >= 3).sum()),
                "signal_seconds": int((part.kind != "none").sum()), "of_which_outlier": int((part.kind == "outlier").sum()),
                "of_which_pair": int((part.kind == "pair").sum()), "tradable_seconds": int(part.side.notna().sum()),
                "events_with_signal": part[part.kind != "none"].expiry.nunique()}
    print("\n" + pd.DataFrame({"first part": counts(train), "second part": counts(test)}).to_string())

    runs = run_length(signals, signals.side.notna())
    ends = runs[(runs > 0) & (runs.shift(-1, fill_value=0) <= runs)]          # length of each finished run
    if len(ends):
        print(f"\nHow long a tradable signal lasts (both parts, {len(ends)} runs): "
              f"1 s {(ends == 1).mean():.0%}, 2 s {(ends == 2).mean():.0%}, "
              f"3 to 9 s {((ends >= 3) & (ends < 10)).mean():.0%}, 10 s or more {(ends >= 10).mean():.0%}")
    print("\nThreshold scan on the first part (edge in cents; P&L in cents per share):")
    scan = []
    for threshold in THRESHOLDS:
        trades, _ = simulate(train, outcomes, threshold)
        scan.append({"threshold_c": round(threshold * 100, 1), **summarise(trades)})
    scan = pd.DataFrame(scan)
    print(scan.round(3).to_string(index=False))
    usable = scan[scan.trades >= MIN_TRAIN_TRADES]
    if len(usable):
        threshold = usable.sort_values("total_cents", ascending=False).iloc[0].threshold_c / 100
        print(f"\nChosen threshold: {threshold * 100:.1f} cents (highest total P&L with at least {MIN_TRAIN_TRADES} trades)")
    else:
        threshold = DEFAULT_THRESHOLD
        print(f"\nNo threshold has {MIN_TRAIN_TRADES} trades in the first part; using the default {threshold * 100:.1f} cents")

    trades, no_entry = simulate(test, outcomes, threshold)
    s = summarise(trades)
    print(f"\nSecond part at {threshold * 100:.1f} cents:")
    print(f"  trades {s['trades']}  (events with a lasting signal but no edge left at the fill: {no_entry}; awaiting resolution: {int(trades.pnl.isna().sum())})")
    if s["trades"]:
        print(f"  hit rate {s['hit_rate']:.1%}")
        interval = (f"95% interval [{s['ci_low']:+.2f}, {s['ci_high']:+.2f}]" if s["trades"] > 1
                    else "no interval from a single trade")
        print(f"  P&L per trade {s['pnl_cents']:+.2f} cents, {interval}, total {s['total_cents']:+.1f} cents")
        done = trades.dropna(subset=["pnl"])
        print(f"  mean edge when seen {done.edge.mean() * 100:.2f} cents, one second later {done.edge_at_entry.mean() * 100:.2f} cents")
        by = done.assign(pnl_c=done.pnl * 100).groupby(["kind", "side"]).pnl_c.agg(["count", "mean", "sum"]).round(2)
        print("\n  By signal type and side:\n  " + by.to_string().replace("\n", "\n  "))
        show = done.assign(expiry=done.expiry.map(fmt_ts), ts=done.ts.map(lambda t: fmt_ts(t)[11:])).round(4)
        print(f"\n  Trades (first 40 of {len(show)}):\n  " + show.head(40).to_string(index=False).replace("\n", "\n  "))
    voted = int((signals.n_votes >= 2).sum())
    SUMMARY.append(f"Events: {len(expiries)} ({fmt_ts(expiries[0])} to {fmt_ts(expiries[-1])} UTC), "
                   f"{int((signals.groupby('expiry').n_votes.max() >= 3).sum())} of them with all three legs voting at some point.")
    SUMMARY.append(f"Signals: on {(signals.kind != 'none').sum() / max(voted, 1):.1%} of the {voted:,} seconds with 2+ votes; "
                   f"{(ends >= PERSIST_S).mean() if len(ends) else 0:.0%} of tradable signals last {PERSIST_S} s or more.")
    if s["trades"] > 1:
        SUMMARY.append(f"Trades (second part, threshold {threshold * 100:.1f} c): {s['trades']}, hit rate {s['hit_rate']:.0%}, "
                       f"P&L {s['pnl_cents']:+.1f} c per trade, 95% interval [{s['ci_low']:+.1f}, {s['ci_high']:+.1f}].")
    else:
        SUMMARY.append(f"Trades (second part, threshold {threshold * 100:.1f} c): {s['trades']}.")
    if s["trades"] < 30:
        SUMMARY[-1] += " Fewer than 30 trades: treat P&L as not yet measured."
    return test, threshold, cut


def price_moves(signals, starts, horizon):
    """For each signal row in `starts`: how the range's price moved over `horizon` seconds after entry.

    mid = move of the range's mid in the signal's direction; trip = buy and sell at bid and ask.
    """
    quotes = signals.set_index(["expiry", "ts"])[[c for c in signals.columns if c.startswith(("bid_", "ask_"))]]
    moves = []
    for r in starts.itertuples():
        first, later = (r.expiry, r.ts + DELAY_S * 1000), (r.expiry, r.ts + (DELAY_S + horizon) * 1000)
        if first not in quotes.index or later not in quotes.index:
            continue
        a, b = quotes.loc[first], quotes.loc[later]
        sign = 1 if r.side == "buy" else -1
        mid_a = (a[f"bid_{r.lo}"] + a[f"ask_{r.lo}"] - a[f"bid_{r.hi}"] - a[f"ask_{r.hi}"]) / 2
        mid_b = (b[f"bid_{r.lo}"] + b[f"ask_{r.lo}"] - b[f"bid_{r.hi}"] - b[f"ask_{r.hi}"]) / 2
        paid = a[f"ask_{r.lo}"] - a[f"bid_{r.hi}"] if sign == 1 else a[f"bid_{r.lo}"] - a[f"ask_{r.hi}"]
        got = b[f"bid_{r.lo}"] - b[f"ask_{r.hi}"] if sign == 1 else b[f"ask_{r.lo}"] - b[f"bid_{r.hi}"]
        moves.append((sign * (mid_b - mid_a), sign * (got - paid), r.edge, r.expiry))
    return pd.DataFrame(moves, columns=["mid", "trip", "edge", "expiry"]).dropna()


def convergence(signals):
    """Does the market move toward the model after a lasting signal? Far less noisy than P&L at expiry."""
    section("2. After a lasting signal, does the price move toward the model's value?")
    print(f"Every signal that has held {PERSIST_S} s is entered one second later and looked at again after 10, 30 and 60 s.")
    print("'mid move' is how far the range's mid moved in the signal's direction; 'round trip' is buying and")
    print("selling at bid and ask. A real mispricing shows a positive mid move of about the edge. Cents, with standard error.\n")
    rows = []
    for threshold in (0.0, 0.02, DEFAULT_THRESHOLD):
        ok = signals.side.notna() & (signals.edge >= threshold)
        starts = signals[run_length(signals, ok) == PERSIST_S]
        for horizon in (10, 30, 60):
            m = price_moves(signals, starts, horizon)
            if len(m) < 3:
                continue
            se = m[["mid", "trip"]].std(ddof=1) / np.sqrt(len(m)) * 100
            rows.append({"min_edge_c": threshold * 100, "after_s": horizon, "signals": len(m), "events": m.expiry.nunique(),
                         "edge_seen_c": m.edge.mean() * 100, "mid_move_c": m.mid.mean() * 100, "mid_se": se["mid"],
                         "round_trip_c": m.trip.mean() * 100, "trip_se": se["trip"]})
    if not rows:
        print("not enough lasting signals yet")
        return
    table = pd.DataFrame(rows)
    print(table.round(2).to_string(index=False))
    best = table[(table.min_edge_c == 0) & (table.after_s == 30)]
    if len(best):
        b = best.iloc[0]
        verdict = ("moves toward the model" if b.mid_move_c > 2 * b.mid_se else
                   "moves away from the model" if b.mid_move_c < -2 * b.mid_se else "no measurable move")
        SUMMARY.append(f"After a lasting signal ({int(b.signals)} signals in {int(b.events)} events): the price moved "
                       f"{b.mid_move_c:+.1f} c toward the model in 30 s (se {b.mid_se:.1f}), against an edge of "
                       f"{b.edge_seen_c:.1f} c seen: {verdict}.")


def error_bar_check(signals):
    """Are the error bars the right size? Compare how much two legs disagree with how much the bars allow."""
    section("3. Are the error bars the right size?")
    print("For every second and pair of legs: z = (vote A - vote B) / combined error bar. If the bars are right,")
    print("z has a spread of about 1 and |z| > 2.5 on about 1% of seconds. The spread is measured robustly")
    print("(from the median), so a few real mispricings do not inflate it.\n")
    rows, every = [], []
    for a, b in (("5m", "15m"), ("15m", "1h"), ("5m", "1h")):
        cols = [f"v_{a}", f"sv_{a}", f"v_{b}", f"sv_{b}"]
        if not all(c in signals for c in cols):
            continue
        d = signals.loc[signals.tau > 60, ["expiry"] + cols].dropna()
        if len(d) < 30:
            continue
        z = (d[f"v_{a}"] - d[f"v_{b}"]) / np.hypot(d[f"sv_{a}"], d[f"sv_{b}"])
        every.append(z)
        rows.append({"pair": f"{a} vs {b}", "seconds": len(z), "events": d.expiry.nunique(), "z_median": z.median(),
                     "z_spread": 1.4826 * (z - z.median()).abs().median(), "share_over_2.5": (z.abs() > Z_LIMIT).mean()})
    if not rows:
        print("not enough seconds with two votes yet")
        return
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    z = pd.concat(every)
    spread = 1.4826 * (z - z.median()).abs().median()
    SUMMARY.append(f"Error bars: contracts disagree by {spread:.2f} error bars in the typical second (1.0 = bars are the right size); "
                   f"beyond {Z_LIMIT} on {(z.abs() > Z_LIMIT).mean():.1%} of seconds (about 1% expected from noise alone).")


def with_limit(signals, limit):
    """The signal table as it would be with a stricter disagreement limit (columns swapped in)."""
    suffix = votes.limit_suffix(limit)
    if not suffix:
        return signals
    return signals.assign(**{col: signals[f"{col}{suffix}"] for col in votes.SIGNAL_COLUMNS})


def limit_scan(signals, outcomes, cut):
    """How the number of signals and trades changes with the disagreement limit, and which limit to use."""
    section("4. Disagreement limit: how many signals and trades each setting gives")
    print(f"The baseline limit is {Z_LIMIT} error bars. It was meant to fire on about 1% of seconds. The limit is chosen")
    print(f"on the first part of the data as the lowest one that signals on at most {TARGET_SIGNAL_RATE:.0%} of seconds with 2+ votes.")
    print(f"Trades use the {DEFAULT_THRESHOLD * 100:.1f} cent threshold. Price move = cents toward the model 30 s after a lasting signal.\n")
    rows, chosen = [], None
    for limit in (Z_LIMIT,) + votes.SCAN_LIMITS:
        t = with_limit(signals, limit)
        first, second = t[t.expiry < cut], t[t.expiry >= cut]
        rate = (first.kind != "none").sum() / max((first.n_votes >= 2).sum(), 1)
        if chosen is None and rate <= TARGET_SIGNAL_RATE:
            chosen = limit
        lasting = t[run_length(t, t.side.notna()) == PERSIST_S]
        moves = price_moves(t, lasting, 30)
        trades, _ = simulate(second, outcomes, DEFAULT_THRESHOLD)
        s = summarise(trades)
        rows.append({"limit": limit, "signal_rate_first": rate,
                     "signal_rate_second": (second.kind != "none").sum() / max((second.n_votes >= 2).sum(), 1),
                     "lasting_signals": len(lasting), "in_events": lasting.expiry.nunique(),
                     "price_move_c": moves.mid.mean() * 100 if len(moves) > 2 else np.nan,
                     "move_se": moves.mid.std(ddof=1) / np.sqrt(len(moves)) * 100 if len(moves) > 2 else np.nan,
                     "edge_seen_c": moves.edge.mean() * 100 if len(moves) > 2 else np.nan,
                     "trades_second": s["trades"], "pnl_c": s["pnl_cents"]})
    table = pd.DataFrame(rows)
    print(table.round(3).to_string(index=False))
    if chosen is None:
        chosen = votes.SCAN_LIMITS[-1]
        print(f"\nNo limit reaches the target on the first part; using the strictest, {chosen}.")
    else:
        print(f"\nChosen limit: {chosen} error bars.")
    row = table[table.limit == chosen].iloc[0]
    move = (f"price move {row.price_move_c:+.1f} c (se {row.move_se:.1f}) against {row.edge_seen_c:.1f} c seen"
            if row.lasting_signals > 2 else "too few lasting signals for a price-move figure")
    SUMMARY.append(f"Limit chosen by signal count: {chosen} error bars (baseline {Z_LIMIT}). Signals on "
                   f"{row.signal_rate_second:.1%} of seconds in the second part, {int(row.lasting_signals)} lasting signals "
                   f"in {int(row.in_events)} events, {int(row.trades_second)} trades; {move}.")


def false_alarms(test, threshold):
    section("5. False alarms in quiet periods")
    voted = test[(test.n_votes >= 2) & test.sd.notna()]
    if voted.empty:
        print("no seconds with 2+ votes")
        return
    limit = voted.sd.quantile(1 / 3)
    print("Quiet = the third of seconds with the lowest realised vol over the previous 60 minutes "
          f"(below {limit * np.sqrt(365 * 86400):.0%} a year).")
    print("In a quiet market the legs should agree, so signals there are treated as false alarms.\n")
    rows = {}
    for name, part in (("quiet", voted[voted.sd <= limit]), ("other", voted[voted.sd > limit])):
        rows[name] = {"seconds": len(part), "signal_rate": (part.kind != "none").mean(),
                      "tradable_rate": (part.side.notna() & (part.edge >= threshold)).mean(),
                      "events": part.expiry.nunique(),
                      "events_with_trade_signal": part[part.side.notna() & (part.edge >= threshold)].expiry.nunique()}
    print(pd.DataFrame(rows).T.round(4).to_string())
    SUMMARY.append(f"False alarms: a signal on {rows['quiet']['signal_rate']:.1%} of quiet seconds, "
                   f"a tradable one on {rows['quiet']['tradable_rate']:.1%}.")


# ============================================================================ step 3: calibration and monotonicity
def calibration(calib, outcomes_by_slug):
    section("6. Calibration: model probability of Up vs what happened, normal and t side by side")
    calib = calib.assign(up=calib.slug.map(outcomes_by_slug)).dropna(subset=["up"])
    calib = calib[(calib.te > 0) & (calib.nu > 2)]
    if calib.empty:
        print("no resolved markets")
        return
    width = calib.sd * np.sqrt(calib.te)                     # standard deviation of the log return still to come
    calib["p_normal"] = stats.norm.sf(calib.x / width)
    calib["p_t"] = stats.t.sf(calib.x / (width * np.sqrt((calib.nu - 2) / calib.nu)), calib.nu)
    print(f"Samples: {len(calib):,} (every {SAMPLE_EVERY_S} s of {calib.slug.nunique()} resolved markets). "
          "Width from realised vol only, no contract prices.")
    print("Samples from one market are not independent, so read the Brier scores as indicative.\n")
    brier = calib.groupby("length").apply(lambda g: pd.Series({
        "samples": len(g), "markets": g.slug.nunique(), "brier_normal": ((g.p_normal - g.up) ** 2).mean(),
        "brier_t": ((g.p_t - g.up) ** 2).mean(), "brier_always_half": ((0.5 - g.up) ** 2).mean()}), include_groups=False)
    brier.loc["all"] = [len(calib), calib.slug.nunique(), ((calib.p_normal - calib.up) ** 2).mean(),
                        ((calib.p_t - calib.up) ** 2).mean(), ((0.5 - calib.up) ** 2).mean()]
    print("Brier score (lower is better):")
    print(brier.round(4).to_string())
    SUMMARY.append(f"Calibration ({calib.slug.nunique()} markets): Brier {brier.loc['all', 'brier_normal']:.4f} normal, "
                   f"{brier.loc['all', 'brier_t']:.4f} t (0.25 = coin flip).")
    table = pd.DataFrame(index=[f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10)])
    for name in ("normal", "t"):
        decile = np.minimum((calib[f"p_{name}"] * 10).astype(int), 9)
        g = calib.groupby(decile)
        table[f"{name}_n"] = g.up.size().reindex(range(10), fill_value=0).values
        table[f"{name}_predicted"] = g[f"p_{name}"].mean().reindex(range(10)).values
        table[f"{name}_happened"] = g.up.mean().reindex(range(10)).values
    print("\nBy predicted probability (a calibrated model has predicted = happened):")
    print(table.round(3).to_string())
    table.to_csv(os.path.join(OUT_DIR, "calibration.csv"))
    svgplot.save(os.path.join(OUT_DIR, "calibration.svg"), [
        {"title": f"{name}: Brier {brier.loc['all', f'brier_{name}']:.4f}", "xlabel": "model probability of Up",
         "ylabel": "share that finished Up", "x": table[f"{name}_predicted"], "y": table[f"{name}_happened"],
         "diagonal": True, "connect": True} for name in ("normal", "t")])
    print(f"\nPlot saved to {os.path.join(OUT_DIR, 'calibration.svg')}, table to calibration.csv")


def monotonicity(signals):
    section("7. Monotonicity: is a lower strike's Yes ever priced below a higher strike's?")
    print("Strikes are compared as ln(line / spot), each leg on its own feed. 'mid' compares mids;")
    print("'executable' means the lower strike's ask is below the higher strike's bid (buy one, sell the other for a credit).\n")
    rows = []
    for a, b in (("5m", "15m"), ("5m", "1h"), ("15m", "1h")):
        if f"bid_{a}" not in signals or f"bid_{b}" not in signals:
            continue
        xa, xb = np.log(signals[f"line_{a}"] / signals[f"spot_{a}"]), np.log(signals[f"line_{b}"] / signals[f"spot_{b}"])
        ok = xa.notna() & xb.notna() & signals[[f"bid_{a}", f"ask_{a}", f"bid_{b}", f"ask_{b}"]].notna().all(axis=1)
        a_low = (xa < xb)[ok]
        part = signals[ok]
        lo_bid, lo_ask = np.where(a_low, part[f"bid_{a}"], part[f"bid_{b}"]), np.where(a_low, part[f"ask_{a}"], part[f"ask_{b}"])
        hi_bid, hi_ask = np.where(a_low, part[f"bid_{b}"], part[f"bid_{a}"]), np.where(a_low, part[f"ask_{b}"], part[f"ask_{a}"])
        mid_bad = (lo_bid + lo_ask) < (hi_bid + hi_ask)
        exec_bad = lo_ask < hi_bid
        rows.append({"pair": f"{a} vs {b}", "seconds": int(ok.sum()), "mid_violations": int(mid_bad.sum()),
                     "mid_rate": mid_bad.mean() if ok.any() else np.nan, "executable_violations": int(exec_bad.sum()),
                     "executable_rate": exec_bad.mean() if ok.any() else np.nan,
                     "events_with_executable": part[exec_bad].expiry.nunique(),
                     "mean_credit_c": (hi_bid - lo_ask)[exec_bad].mean() * 100 if exec_bad.any() else np.nan})
    print(pd.DataFrame(rows).round(4).to_string(index=False) if rows else "no pairs")
    if rows:
        SUMMARY.append("Strikes out of order at executable prices: " + ", ".join(
            f"{r['pair']} {r['executable_violations']} s of {r['seconds']:,}" for r in rows) + ".")


# ============================================================================ main
def run(start, end):
    print(f"Backtest {fmt_ts(start)} to {fmt_ts(end)} UTC   (data: {config.DATA_DIR}, run at {fmt_ts(now_ms())} UTC)")
    print(f"nu per horizon in minutes: {votes.load_nu()}")
    print(f"Error bars include spot-price staleness: {votes.STALENESS_ERR}")
    if start >= end:
        print("\nThis period has not started yet.")
        return
    signals, calib = load_days(start, end)
    if signals.empty:
        print("\nNo event has expired in this period yet.")
        return
    resolutions = load("resolutions", start, now_ms()).drop_duplicates("slug", keep="last")
    resolutions = resolutions[resolutions.outcome.isin(["Up", "Down"])]
    up = (resolutions.outcome == "Up").astype(float)
    outcomes = dict(zip(zip(resolutions.close_ts, resolutions.length), up))
    outcomes_by_slug = dict(zip(resolutions.slug, up))
    test, threshold, cut = trading_report(signals, outcomes)
    convergence(signals)
    error_bar_check(signals)
    limit_scan(signals, outcomes, cut)
    false_alarms(test, threshold)
    calibration(calib, outcomes_by_slug)
    monotonicity(signals)
    section("Summary")
    print("\n".join(SUMMARY))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    votes.STALENESS_ERR = "--brief-bars" not in sys.argv
    if len(args) >= 2:
        start, end = (int(pd.Timestamp(a, tz="UTC").timestamp() * 1000) for a in args[:2])
    else:
        start, end = int(load("health", 0, now_ms(), columns=["recv_ts"]).recv_ts.min()), now_ms()
    end = min(end, now_ms() - 60_000)
    buffer, stdout = io.StringIO(), sys.stdout

    class Tee:
        def write(self, text):
            stdout.write(text)
            buffer.write(text)

        def flush(self):
            stdout.flush()

    sys.stdout = Tee()
    try:
        run(start, end)
    finally:
        sys.stdout = stdout
    folder = os.path.join(config.DATA_DIR, "reports")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"backtest_{pd.Timestamp(now_ms(), unit='ms').strftime('%Y-%m-%d_%H%M')}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(buffer.getvalue())
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
