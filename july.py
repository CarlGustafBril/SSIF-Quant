"""The same rules on the teammate's July 2026 tick recording (shared/data/events).

    python july.py build          # per-second signal tables for every shared expiry (a few minutes, parallel)
    python july.py check          # how good the stand-ins are: clock, quotes, lines, error-bar fit

One file per shared expiry holds the order-book updates of each contract for the 15 minutes
before it. This module turns each file into the same per-second table votes.event_table makes
from our own recorder, and decides every second with the same code (votes.decide_rows).

What differs from our own recording, and how it is handled:

  * Clock. The recording PC's clock ran 9 to 17 s fast, drifting about a second a day. Binance rows
    carry the exchange's own time, so each file's offset is measured (to about 10 ms) and removed.
  * No Chainlink price. In July the 5m and 15m contracts settled on the Chainlink spot price at the
    window's two ends (no averaging), and the dataset has no Chainlink feed. The official line of
    every 5m contract is the Chainlink price at a 5-minute boundary, so at each boundary the gap
    between Binance and Chainlink is known exactly. In between, Chainlink is taken as Binance
    minus that gap. Measured on our November recording, where both prices exist, this is off by
    about $5 (the gap wobbles but does not drift), the same as our live Chainlink price being 2 s
    old. It is therefore given an age of 3 s in the error bars (1 s for Binance's own delay).
  * Lines and outcomes are the official ones (scripts/fetch_july_reference.py). Live, the line is
    the feed value at the open, which our recorder reproduces to the cent.
  * Binance prices are the public 1-second candles; at second t the last candle used closed at
    t - 1 s, as it would have arrived live.

Nothing is written into shared/. Tables go to <POLYDATA_DIR>/analysis/july_tick_<code tag>.parquet.
"""
import glob
import hashlib
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

import backtest
import votes
from recorder import config

SHARED = os.path.join(config.DATA_DIR, "shared", "data")
REFERENCE = os.path.join(config.DATA_DIR, "reference")
OUT_DIR = os.path.join(config.DATA_DIR, "analysis")
WINDOWS = {"5m": 0, "15m": 0, "1h": 0}          # July rules: both ends are spot prices, nothing is averaged
LENGTH_S = {"5m": 300, "15m": 900, "1h": 3600}
BINANCE_AGE_MS = 1000                            # the last closed 1-second candle
CHAINLINK_AGE_MS = 3000                          # Binance's delay plus the stand-in's error, as an age
DEAD_AFTER_MS = 5000                             # no row of any kind for this long: the recorder was down
TICK = 0.01
_REF = {}


# ============================================================================ reference data
def reference():
    """Official lines and outcomes, Binance 1-second candles and the Chainlink price at 5-minute boundaries."""
    if _REF:
        return _REF
    lines = pd.read_parquet(os.path.join(REFERENCE, "gamma_lines.parquet"))
    candles = pd.read_parquet(os.path.join(REFERENCE, "binance_1s.parquet")).set_index("open_ts").sort_index()
    lines["open_ts"] = lines.close_ts - lines.length.map(LENGTH_S)      # the fetched open time has holes
    lines = lines.astype({"open_ts": "int64", "close_ts": "int64"})
    chain = lines[lines.length.isin(["5m", "15m"])]
    # a window's line is the Chainlink price at its open, its final price the one at its close
    at = pd.concat([pd.Series(chain.line.values, index=chain.open_ts.values),
                    pd.Series(chain.final.values, index=chain.close_ts.values)]).dropna()
    at = at.groupby(level=0).first().sort_index()
    closes = candles.close.reindex(at.index * 1000 - 1000)           # the candle that closed at the boundary
    gap = pd.Series(closes.values - at.values, index=(at.index * 1000).astype("int64")).dropna()
    _REF.update(lines=lines.set_index("slug"), candles=candles, chainlink_at=at,
                gap=pd.DataFrame({"recv_ts": gap.index.values, "gap": gap.values}),
                hourly={int(r.close_ts): r.slug for r in lines[lines.length == "1h"].itertuples()})
    return _REF


def outcomes():
    """{(expiry in ms, length): 1.0 if Up else 0.0} for every July contract with an official result."""
    lines = reference()["lines"].dropna(subset=["up"])
    lines = lines[lines.up.isin([0.0, 1.0])]
    return {(int(c) * 1000, l): float(u) for c, l, u in zip(lines.close_ts, lines.length, lines.up)}


def event_files():
    return sorted(p for p in glob.glob(os.path.join(SHARED, "events", "20*.parquet")))


def expiry_of(path):
    return int(pd.to_datetime(os.path.basename(path)[:15], format="%Y%m%d_%H%M%S", utc=True).timestamp())


# ============================================================================ one event
def clock_offset_ms(rows):
    """How far the recording PC's clock was ahead, from the Binance rows (which carry exchange time)."""
    b = rows[rows.rowtype == "binance"]
    return float((b.ts_ms - b.ex_ms).median()) if len(b) else np.nan


def event_table(path, nu_by_horizon=None):
    """One row per second of a July event, in the layout of votes.event_table. None if unusable."""
    ref = reference()
    nu_by_horizon = nu_by_horizon or votes.load_nu()
    expiry_s = expiry_of(path)
    expiry = expiry_s * 1000
    rows = pd.read_parquet(path, columns=["ts_ms", "rowtype", "leg", "bid", "ask", "ex_ms"])
    offset = clock_offset_ms(rows)
    if not np.isfinite(offset):
        return None
    rows["ts"] = (rows.ts_ms - offset).round().astype("int64")
    books = rows[rows.rowtype == "book"].sort_values("ts", kind="stable")
    names = [n for n in ("5m", "15m", "1h") if (books.leg == n).any()]
    opens = {n: expiry - LENGTH_S[n] * 1000 for n in names}
    if len(names) < 2:
        return None
    start = max(sorted(opens.values())[-2], int(rows.ts.min()) // 1000 * 1000 + 1000)
    grid = np.arange(start, expiry, 1000, dtype="int64")
    if not len(grid):
        return None
    candles = ref["candles"]
    table = pd.DataFrame({"expiry": expiry, "ts": grid, "tau": (expiry - grid) / 1000})
    table["sd"] = votes.realised_sd(grid, candles)
    table["sd_fast"] = votes.realised_sd(grid, candles, returns=30, step_s=10)
    binance = candles.close.reindex(grid - 2000).values                 # closed at t - 1 s
    gap = votes.asof(grid, ref["gap"], ["gap"])
    gap_ok = (gap.ts - gap.recv_ts).values <= 600_000                   # a boundary at most 10 minutes back
    chainlink = np.where(gap_ok, binance - gap.gap.values, np.nan)
    every = rows[["ts"]].sort_values("ts").rename(columns={"ts": "recv_ts"})
    alive = (grid - votes.asof(grid, every.assign(x=0), ["x"]).recv_ts.values) <= DEAD_AFTER_MS
    lines = ref["lines"]
    for n in names:
        q = books.loc[books.leg == n, ["ts", "bid", "ask"]].rename(columns={"ts": "recv_ts"})
        quotes = votes.asof(grid, q, ["bid", "ask"])
        live = (grid >= opens[n]) & alive
        if n == "1h":
            spot, age = binance, BINANCE_AGE_MS
            line = candles.open.get(opens[n], np.nan)
        else:
            spot, age = chainlink, CHAINLINK_AGE_MS
            line = ref["chainlink_at"].get(opens[n] // 1000, np.nan)
        table[f"bid_{n}"] = quotes.bid.where(live).values
        table[f"ask_{n}"] = quotes.ask.where(live).values
        table[f"quote_age_{n}"] = (quotes.ts - quotes.recv_ts).where(live).values
        table[f"spot_{n}"] = spot
        table[f"feed_age_{n}"] = float(age)
        table[f"line_{n}"] = line
        table[f"tick_{n}"] = TICK
    decided = votes.decide_rows(table, names, nu_by_horizon, windows=WINDOWS)
    result = pd.concat([table, pd.DataFrame(decided)], axis=1)
    for n in names:
        for col in (f"x_{n}", f"v_{n}", f"sv_{n}"):
            if col not in result:
                result[col] = np.nan
    result["clock_offset_ms"] = offset
    return result


def _build_one(path):
    try:
        return event_table(path)
    except Exception as e:                       # one bad file must not stop the run; it is reported
        return f"{os.path.basename(path)}: {type(e).__name__}: {e}"


def table_path(kind="tick"):
    with open(os.path.abspath(__file__), "rb") as f:          # rebuilt when the rules or this file change
        tag = hashlib.md5(backtest.cache_tag().encode() + f.read()).hexdigest()[:8]
    return os.path.join(OUT_DIR, f"july_{kind}_{tag}.parquet")


def build(workers=6):
    files = event_files()
    print(f"building {len(files)} July events with {workers} processes ...", flush=True)
    with ProcessPoolExecutor(workers) as pool:
        results = list(pool.map(_build_one, files, chunksize=4))
    failed = [r for r in results if isinstance(r, str)]
    tables = [r for r in results if isinstance(r, pd.DataFrame)]
    for f in failed[:20]:
        print("  failed:", f)
    signals = pd.concat(tables, ignore_index=True)
    for col in signals.columns:
        if col.split("_z")[0] in ("kind", "outlier", "lo", "hi", "side"):
            signals[col] = signals[col].astype("string")
    os.makedirs(OUT_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(OUT_DIR, "july_tick_*.parquet")):
        os.remove(old)
    signals.to_parquet(table_path())
    print(f"{signals.expiry.nunique()} events, {len(signals):,} seconds, {len(failed)} files failed, "
          f"{len(files) - len(failed) - len(tables)} skipped -> {table_path()}")
    return signals


def load():
    """The July signal table, built if this version of the rules has none yet."""
    with votes_windows():
        return pd.read_parquet(table_path()) if os.path.exists(table_path()) else build()


class votes_windows:
    """Inside this block the rules use July's settlement (no averaging). Needed by variants.redecide."""
    def __enter__(self):
        self.saved = dict(votes.AVG_WINDOW_S)
        votes.AVG_WINDOW_S.update(WINDOWS)

    def __exit__(self, *exc):
        votes.AVG_WINDOW_S.update(self.saved)


# ============================================================================ the 3-second polls
# The same PC also polled the best bid and ask of every contract about every 3 seconds, gap-free
# from 2026-07-02 to 07-22: three times as many expiries as the tick extracts, but coarser.
# Each poll row carries the Binance price fetched in the same cycle; against Binance's own
# candles it is about 1.3 s older than the row's (corrected) time stamp.
REST_FILE = os.path.join(SHARED, "orderbook_rest", "orderbook_rest_dedup.parquet")
REST_BINANCE_AGE_MS = 2000
REST_CHAINLINK_AGE_MS = 4000
REST_MAX_QUOTE_AGE_MS = 12_000
CLOCK_REF_MS, CLOCK_REF_OFFSET_MS, CLOCK_DRIFT = 1783794600000, 9225.0, 1.045 / 86400     # measured on the tape


def rest_clock_offset_ms(ts_ms):
    """The recording PC's clock error at a given (uncorrected) time: a straight line through the
    offsets measured on the tick tape (9.2 s on 07-11 18:30, 1.045 s more per day), not below 0.5 s.
    The polls' own Binance price agrees with this line to within a second on every day."""
    return np.maximum(CLOCK_REF_OFFSET_MS + (np.asarray(ts_ms, dtype="float64") - CLOCK_REF_MS) * CLOCK_DRIFT, 500.0)


def rest_polls():
    """All 5m/15m/1h poll rows from 2026-07-02 on, with corrected time, grouped by expiry."""
    ob = pd.read_parquet(REST_FILE, columns=["timestamp", "duration_label", "expiry", "bid", "ask", "btc_spot"])
    ob = ob[ob.duration_label.isin(LENGTH_S)]
    epoch = pd.Timestamp(0, tz="UTC")
    raw = (pd.to_datetime(ob.timestamp, utc=True) - epoch) // pd.Timedelta(milliseconds=1)
    ob = ob.assign(recv_ts=(raw - rest_clock_offset_ms(raw)).round().astype("int64"),
                   expiry=(pd.to_datetime(ob.expiry, utc=True) - epoch) // pd.Timedelta(milliseconds=1))
    ob = ob[ob.recv_ts >= int(pd.Timestamp("2026-07-02", tz="UTC").timestamp() * 1000)]
    ob = ob[(ob.expiry - ob.recv_ts <= 900_000) & (ob.recv_ts < ob.expiry)].sort_values("recv_ts", kind="stable")
    return {int(e): g.drop(columns=["timestamp", "expiry"]) for e, g in ob.groupby("expiry") if g.duration_label.nunique() >= 2}


def rest_event_table(args):
    """Like event_table, from poll rows. Quotes older than 12 s are dropped."""
    expiry, polls = args
    ref = reference()
    nu_by_horizon = votes.load_nu()
    names = [n for n in ("5m", "15m", "1h") if (polls.duration_label == n).any()]
    opens = {n: expiry - LENGTH_S[n] * 1000 for n in names}
    grid = np.arange(sorted(opens.values())[-2], expiry, 1000, dtype="int64")
    candles = ref["candles"]
    table = pd.DataFrame({"expiry": expiry, "ts": grid, "tau": (expiry - grid) / 1000})
    table["sd"] = votes.realised_sd(grid, candles)
    table["sd_fast"] = votes.realised_sd(grid, candles, returns=30, step_s=10)
    spot = votes.asof(grid, polls, ["btc_spot"])
    binance = np.where((spot.ts - spot.recv_ts).values <= REST_MAX_QUOTE_AGE_MS, spot.btc_spot.values, np.nan)
    gap = votes.asof(grid, ref["gap"], ["gap"])
    chainlink = np.where((gap.ts - gap.recv_ts).values <= 600_000, binance - gap.gap.values, np.nan)
    for n in names:
        quotes = votes.asof(grid, polls[polls.duration_label == n], ["bid", "ask"])
        age = (quotes.ts - quotes.recv_ts).values
        live = (grid >= opens[n]) & (age <= REST_MAX_QUOTE_AGE_MS)
        if n == "1h":
            leg_spot, feed_age, line = binance, REST_BINANCE_AGE_MS, candles.open.get(opens[n], np.nan)
        else:
            leg_spot, feed_age, line = chainlink, REST_CHAINLINK_AGE_MS, ref["chainlink_at"].get(opens[n] // 1000, np.nan)
        table[f"bid_{n}"] = quotes.bid.where(live).values
        table[f"ask_{n}"] = quotes.ask.where(live).values
        table[f"quote_age_{n}"] = np.where(live, age, np.nan)
        table[f"spot_{n}"] = leg_spot
        table[f"feed_age_{n}"] = float(feed_age)
        table[f"line_{n}"] = line
        table[f"tick_{n}"] = TICK
    decided = votes.decide_rows(table, names, nu_by_horizon, windows=WINDOWS)
    result = pd.concat([table, pd.DataFrame(decided)], axis=1)
    for n in names:
        for col in (f"x_{n}", f"v_{n}", f"sv_{n}"):
            if col not in result:
                result[col] = np.nan
    return result


def build_rest(workers=6):
    polls = rest_polls()
    print(f"building {len(polls)} July events from the polls with {workers} processes ...", flush=True)
    with ProcessPoolExecutor(workers) as pool:
        tables = list(pool.map(rest_event_table, sorted(polls.items()), chunksize=8))
    signals = pd.concat(tables, ignore_index=True)
    for col in signals.columns:
        if col.split("_z")[0] in ("kind", "outlier", "lo", "hi", "side"):
            signals[col] = signals[col].astype("string")
    for old in glob.glob(os.path.join(OUT_DIR, "july_rest_*.parquet")):
        os.remove(old)
    signals.to_parquet(table_path("rest"))
    print(f"{signals.expiry.nunique()} events, {len(signals):,} seconds -> {table_path('rest')}")
    return signals


def load_rest():
    return pd.read_parquet(table_path("rest")) if os.path.exists(table_path("rest")) else build_rest()


# ============================================================================ checks
def check():
    signals = load()
    out = outcomes()
    print(f"Events {signals.expiry.nunique()}, from {backtest.fmt_ts(signals.expiry.min())} to "
          f"{backtest.fmt_ts(signals.expiry.max())} UTC; seconds {len(signals):,}")
    per = signals.groupby("expiry")
    print(f"Clock offset removed: {per.clock_offset_ms.first().min() / 1000:.1f} to {per.clock_offset_ms.first().max() / 1000:.1f} s")
    legs = [n for n in ("5m", "15m", "1h") if f"bid_{n}" in signals]
    for n in legs:
        live = signals[f"bid_{n}"].notna() | signals[f"ask_{n}"].notna()
        has = signals.groupby("expiry")[f"line_{n}"].first().notna()
        known = sum((e, n) in out for e in signals.loc[live, "expiry"].unique())
        print(f"  {n}: quoted on {live.mean():.1%} of seconds, median quote age "
              f"{signals.loc[live, f'quote_age_{n}'].median() / 1000:.2f} s, line known for {has.sum()} events, "
              f"outcome known for {known} events, votes on {signals[f'v_{n}'].notna().mean():.1%} of seconds")
    print(f"Seconds with 2+ votes: {(signals.n_votes >= 2).sum():,}; with 3: {(signals.n_votes >= 3).sum():,}; "
          f"signal on {(signals.kind != 'none').sum() / max((signals.n_votes >= 2).sum(), 1):.1%} of them")
    backtest.error_bar_check(signals)
    print("\n".join(backtest.SUMMARY))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "build":
        build()
        build_rest()
    elif command == "check":
        check()
    else:
        sys.exit(__doc__)
