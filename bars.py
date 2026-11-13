"""Measured error bars for the reference price, and the Chainlink price moved forward with Binance.

    python bars.py measure      # the measurements behind MEASURED, from our own recording and July
    python bars.py nowcast      # build the recorder's signal table with the moved-forward Chainlink price
    python bars.py check        # the bars and the moved-forward price on seconds recorded after the measurement

The frozen rules allow for the age of our reference price with a formula: spot x volatility x
sqrt(age). The variant "measured_bars" (variants.py) replaces the formula with what the error was
measured to be, for each kind of reference price:

    naive         the last Chainlink price received (what the frozen rules use on our recording)
    nowcast       that price moved forward by Binance's move since it was stamped
    standin_twap  Binance minus the gap at the last 5-minute boundary, gap between 60 s averages (the public tape)
    standin_spot  the same with the gap between prices at the boundary (July)
    binance       the last closed 1-second Binance candle (the hourly contract)
    gap           how much Binance minus Chainlink changes over 5 minutes: added to the hourly contract,
                  whose line and settlement are Binance prices while the others are Chainlink prices

Each error is described by two numbers: a fixed part in dollars and a part that grows with
volatility,  bar = sqrt(a^2 + (k x spot x volatility)^2),  volatility per sqrt-second as in the
frozen rules. The bar is sized for the job it does: errors beyond 2.5 bars are a signal, so the
bar is the size at which the measured error exceeds 2.5 bars as often as a normal error would
(1.2% of seconds). These errors have fat tails, so this bar is wider than the typical error and
narrower than the largest ones would suggest.

Measured only on our own recording (to CAL_END) and on July. No autumn or October-November data.
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

import votes
from recorder import config
from recorder.reader import fmt_ts, load

CAL_END = int(pd.Timestamp("2026-11-13T09:00", tz="UTC").timestamp() * 1000)
CHECK_END = int(pd.Timestamp("2026-11-13T11:00", tz="UTC").timestamp() * 1000)     # the test on new seconds, see HISTORY.md
Z = votes.Z_LIMIT
VOL_BINS = 5
KINDS = ("naive", "nowcast", "standin_twap", "standin_spot", "binance", "gap")
OUT_DIR = os.path.join(config.DATA_DIR, "analysis")

# (a in dollars, k in sqrt-seconds), from `python bars.py measure` on 2026-11-13. See results/HISTORY.md.
MEASURED = {
    "naive": (3.26, 1.339),
    "nowcast": (2.23, 0.898),
    "standin_twap": (2.13, 0.676),
    "standin_spot": (5.06, 0.178),
    "binance": (3.15, 1.029),
    "gap": (2.97, 0.964),
}


def spot_error(kind, spot, sd_now):
    """The measured error bar of a reference price of this kind, in dollars."""
    a, k = MEASURED[kind]
    return float(np.hypot(a, k * spot * sd_now))


# ============================================================================ measuring
def bar_size(errors, z=Z):
    """The bar at which |error| exceeds z bars as often as a normal error would."""
    e = np.abs(np.asarray(errors, dtype=float))
    e = e[np.isfinite(e)]
    return float(np.quantile(e, 1 - 2 * stats.norm.sf(z)) / z) if len(e) else np.nan


def rms(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    return float(np.sqrt(np.mean(x ** 2))) if len(x) else np.nan


def robust_sd(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    return float(1.4826 * np.median(np.abs(x - np.median(x)))) if len(x) else np.nan


def recorder_errors(end=CAL_END):
    """Per second of our recording: each kind of reference price minus the Chainlink (or Binance) price at that second."""
    start = int(load("health", 0, end, columns=["recv_ts"]).recv_ts.min())
    ch = load("chainlink", start, end, columns=["recv_ts", "src_ts", "stream", "value", "backfill"])
    bi = load("binance", start, end, columns=["recv_ts", "open_ts", "close", "closed", "backfill"])
    spot = ch[ch.stream == "spot"].sort_values("recv_ts", kind="stable")
    chain_at = spot.drop_duplicates("src_ts").set_index("src_ts").value.sort_index()
    candles = bi[bi.closed].sort_values("recv_ts", kind="stable")
    candles = candles.assign(price_ts=candles.open_ts + 1000)            # a candle's close is the price one second after its open
    binance_at = candles.drop_duplicates("price_ts").set_index("price_ts").close.sort_index()
    by_open = candles.drop_duplicates("open_ts").set_index("open_ts").sort_index()

    grid = np.arange(start // 1000 * 1000 + 3_700_000, end, 1000, dtype="int64")       # after an hour, so volatility exists
    left = pd.DataFrame({"ts": grid})
    c = pd.merge_asof(left, spot[~spot.backfill][["recv_ts", "src_ts", "value"]], left_on="ts", right_on="recv_ts")
    b = pd.merge_asof(left, candles[~candles.backfill][["recv_ts", "price_ts", "close"]], left_on="ts", right_on="recv_ts")
    d = pd.DataFrame({"ts": grid, "chain_src": c.src_ts, "chain": c.value, "binance_ts": b.price_ts, "binance": b.close})
    d["chain_true"], d["binance_true"] = d.ts.map(chain_at), d.ts.map(binance_at)
    d["binance_then"] = d.chain_src.map(binance_at)
    sd = votes.realised_sd(grid, by_open)
    d["unit"] = d.chain_true * np.fmax(sd, votes.realised_sd(grid, by_open, returns=30, step_s=10))    # dollars per sqrt-second
    d = d.dropna()
    d = d[(d.ts - d.chain_src <= votes.MAX_FEED_AGE_MS) & (d.ts - d.binance_ts <= votes.MAX_FEED_AGE_MS)].copy()

    full = binance_at.reindex(range(int(binance_at.index.min()), int(binance_at.index.max()) + 1000, 1000))
    chain_full = chain_at.reindex(full.index)
    gap_now = full - chain_full
    gap_avg = full.rolling(60, min_periods=50).mean() - chain_full.rolling(60, min_periods=50).mean()
    boundary = d.ts // 300_000 * 300_000
    known = (d.ts - 1000).map(binance_at)                                 # the candle the stand-in uses: closed at t - 1 s
    d["naive"] = d.chain - d.chain_true
    d["nowcast"] = moved_forward(d.chain, d.chain_src, d.binance, d.binance_ts, d.binance_then) - d.chain_true
    d["standin_twap"] = known - boundary.map(gap_avg) - d.chain_true
    d["standin_spot"] = known - boundary.map(gap_now) - d.chain_true
    d["binance"] = d.binance - d.binance_true
    d["gap"] = (d.ts + 300_000).map(gap_now) - d.ts.map(gap_now)
    d["source"] = "recorder"
    return d[["ts", "unit", "source", *KINDS]]


def july_gap_changes():
    """Change of Binance minus Chainlink between consecutive 5-minute boundaries of the July recording."""
    import july
    ref = july.reference()
    gap = ref["gap"].set_index("recv_ts").gap
    first, last = (int(pd.Timestamp(t, tz="UTC").timestamp() * 1000) for t in ("2026-07-11", "2026-07-23"))
    gap = gap[(gap.index >= first) & (gap.index < last)]
    grid = gap.index.values.astype("int64")
    sd = np.fmax(votes.realised_sd(grid, ref["candles"]), votes.realised_sd(grid, ref["candles"], returns=30, step_s=10))
    unit = ref["candles"].close.reindex(grid - 1000).values * sd
    change = gap.reindex(gap.index + 300_000).values - gap.values
    return pd.DataFrame({"ts": grid, "unit": unit, "source": "july", "gap": change}).dropna()


def fit(errors, unit, bins=VOL_BINS):
    """(a, k) with bar^2 = a^2 + (k * unit)^2, through the bar measured in each volatility bin."""
    d = pd.DataFrame({"e": np.asarray(errors, dtype=float), "u": np.asarray(unit, dtype=float)}).dropna()
    d["bin"] = pd.qcut(d.u, bins, labels=False, duplicates="drop")
    points = d.groupby("bin").apply(lambda p: pd.Series({"u": np.sqrt(np.mean(p.u ** 2)), "bar": bar_size(p.e)}), include_groups=False)
    weight = 1 / points.bar.values ** 2          # so that every bin counts by its relative miss, not its dollars squared
    design = np.column_stack([np.ones(len(points)), points.u ** 2]) * weight[:, None]
    coef, *_ = np.linalg.lstsq(design, points.bar.values ** 2 * weight, rcond=None)
    if coef[0] < 0:                              # no fixed part: the error is proportional to volatility
        coef = [0.0, float(np.sum(points.bar ** 2 * points.u ** 2) / np.sum(points.u ** 4))]
    if coef[1] < 0:                              # no growth with volatility: a fixed number of dollars
        coef = [float(np.mean(points.bar ** 2)), 0.0]
    return float(np.sqrt(coef[0])), float(np.sqrt(coef[1])), points


def measure():
    pd.set_option("display.width", 220)
    d = recorder_errors()
    print(f"Our recording, {fmt_ts(d.ts.min())} to {fmt_ts(d.ts.max())} UTC: {len(d):,} seconds. "
          f"spot x volatility: median ${d.unit.median():.2f} per sqrt-second.\n")
    formula = {"naive": "sqrt(2 s) = 1.41", "nowcast": "", "standin_twap": "sqrt(4 s) = 2.00", "standin_spot": "sqrt(3 s) = 1.73",
               "binance": "sqrt(1 s) = 1.00 (tape: 1.41)", "gap": "none"}
    july_gap = july_gap_changes()
    rows, fitted = [], {}
    for kind in KINDS:
        data = d
        if kind == "gap":                        # both recordings
            data = pd.concat([d[["unit", "source", kind]], july_gap[["unit", "source", kind]]])
        if kind == "standin_spot":               # used on July only, so measured on July: see the note printed below
            data = july_gap.rename(columns={"gap": kind})
        a, k, points = fit(data[kind], data.unit)
        fitted[kind] = (round(a, 2), round(k, 3))
        bar = np.hypot(a, k * data.unit)
        beyond = (data[kind].abs() > Z * bar)[data[kind].notna()].mean()
        print(f"  {kind}: bar by volatility bin  " + "  ".join(f"${u:.1f}: ${b:.2f}" for u, b in zip(points.u, points.bar)))
        rows.append({"kind": kind, "seconds": int(data[kind].notna().sum()), "typical $": robust_sd(data[kind]), "rms $": rms(data[kind]),
                     "bar $ (one number)": bar_size(data[kind]), "a $": a, "k": k, "bar $ at median vol": float(np.hypot(a, k * data.unit.median())),
                     f"beyond {Z} bars": beyond, "frozen formula k": formula[kind]})
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    print(f"\n'bar' is the size at which the error exceeds {Z} bars on {2 * stats.norm.sf(Z):.1%} of seconds, as a normal error would.")
    print("standin_spot is measured on July itself: July has no Chainlink feed, only its price at each 5-minute boundary, so")
    print("the stand-in's error is known where it is largest, just before the next boundary (the change of the gap over")
    print("5 minutes). The same stand-in built from our own recording, where every second can be checked:")
    a, k, _ = fit(d.standin_spot, d.unit)
    print(f"  a ${a:.2f}, k {k:.3f}: bar ${np.hypot(a, k * july_gap.unit.median()):.2f} at July's median volatility, "
          f"against ${np.hypot(*np.multiply(fitted['standin_spot'], (1, july_gap.unit.median()))):.2f} measured on July")
    print("\nMEASURED = {")
    for kind, (a, k) in fitted.items():
        print(f'    "{kind}": ({a}, {k}),')
    print("}")
    return fitted


def check(start=CAL_END, end=CHECK_END):
    """The measured bars on seconds they were not measured on (specification in results/HISTORY.md)."""
    d = recorder_errors(end)
    d = d[d.ts >= start]
    print(f"Our recording, {fmt_ts(d.ts.min())} to {fmt_ts(d.ts.max())} UTC: {len(d):,} seconds not used in the measurement. "
          f"spot x volatility: median ${d.unit.median():.2f}.\n")
    rows = []
    for kind in ("naive", "nowcast", "binance"):
        a, k = MEASURED[kind]
        bar = np.hypot(a, k * d.unit)
        rows.append({"kind": kind, "typical $": robust_sd(d[kind]), "rms $": rms(d[kind]), "bar $ on these seconds": bar_size(d[kind]),
                     "measured bar $ (median)": float(bar.median()), f"beyond {Z} measured bars": float((d[kind].abs() > Z * bar).mean())})
    table = pd.DataFrame(rows)
    print(table.round(3).to_string(index=False))
    naive, moved = table.set_index("kind")["bar $ on these seconds"][["naive", "nowcast"]]
    print(f"\nMoved forward against last received: bar {1 - moved / naive:.0%} smaller, rms {1 - rms(d.nowcast) / rms(d.naive):.0%} smaller.")
    return table


# ============================================================================ the Chainlink price moved forward
def moved_forward(chain, chain_src, binance, binance_ts, binance_then):
    """Chainlink's last price times Binance's move since that price was stamped.

    binance_then is Binance's price at the Chainlink stamp. When Binance's last price is not newer
    than the Chainlink stamp, or its price then is unknown, the Chainlink price is used as it is.
    """
    ok = (np.asarray(binance_ts) > np.asarray(chain_src)) & np.isfinite(binance_then) & (np.asarray(binance_then) > 0)
    return np.where(ok, np.asarray(chain) * np.asarray(binance) / np.where(ok, binance_then, 1.0), chain)


def nowcast_leg(grid, market, feeds, w):
    """votes.twap_leg_reference with the Chainlink spot moved forward by Binance. Returns the spot per grid time."""
    spot = votes.asof(grid, feeds["spot_live"], ["src_ts", "value"])
    candles = votes.asof(grid, feeds["candles_live"], ["open_ts", "close"])
    then = feeds["candles_by_open"].close.reindex(spot.src_ts.values - 1000).values       # Binance at the Chainlink stamp
    ref = moved_forward(spot.value.values, spot.src_ts.values, candles.close.values, candles.open_ts.values + 1000, then).astype(float)
    age = (spot.ts - spot.src_ts).values
    by_src = feeds["spot_by_src"]
    window = by_src[(by_src.src_ts > market.close_ts - w * 1000) & (by_src.src_ts <= market.close_ts)]
    for n in np.flatnonzero(grid > market.close_ts - w * 1000):          # inside the averaging window, as the frozen rules do
        seen = window[(window.src_ts <= grid[n]) & (window.recv_ts <= grid[n])]
        tau = (market.close_ts - grid[n]) / 1000
        if len(seen):
            ref[n] = ((w - tau) * seen.value.mean() + tau * ref[n]) / w
    return np.where(age <= votes.MAX_FEED_AGE_MS, ref, np.nan)


def nowcast_path():
    return os.path.join(OUT_DIR, "signals_nowcast.parquet")


def nowcast_table(signals):
    """Our recording's signal table with the 5m and 15m spot replaced by the moved-forward price."""
    import backtest
    out = signals.copy()
    day = backtest.DAY
    for day_start in range(int(signals.expiry.min()) // day * day, int(signals.expiry.max()) + 1, day):
        feeds = votes.load_feeds(day_start - 2 * 3_600_000, day_start + day + 10_000)
        markets = load("markets", day_start - 2 * day, day_start + day).drop_duplicates("slug")
        markets = markets[(markets.close_ts >= day_start) & (markets.close_ts < day_start + day) & markets.length.isin(["5m", "15m"])]
        for m in markets.itertuples(index=False):
            rows = out.index[out.expiry == m.close_ts]
            if not len(rows) or f"spot_{m.length}" not in out:
                continue
            grid = out.loc[rows, "ts"].values.astype("int64")
            out.loc[rows, f"spot_{m.length}"] = nowcast_leg(grid, m, feeds, votes.AVG_WINDOW_S[m.length])
    return out


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "measure":
        measure()
    elif command == "check":
        check()
    elif command == "nowcast":
        import evidence
        signals, _, _ = evidence.dataset("november")
        table = nowcast_table(signals)
        table.to_parquet(nowcast_path())
        changed = (table.spot_15m - signals.spot_15m).abs()
        print(f"{table.expiry.nunique()} events, {len(table):,} seconds -> {nowcast_path()}")
        print(f"15m spot moved by a median of ${changed.median():.2f} (90th percentile ${changed.quantile(0.9):.2f})")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
