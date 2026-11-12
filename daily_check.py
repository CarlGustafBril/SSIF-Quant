"""Daily check of the recorder's data for one UTC day (default: yesterday).

    python daily_check.py              # yesterday
    python daily_check.py 2026-11-12   # a given day

Prints the report and saves it to <POLYDATA_DIR>/reports/daily_<date>.txt. Sections:
  1. Gaps per stream
  2. Book coverage per contract window (first and last quote vs open and close)
  3. Events: expiries with 2 and 3 legs, and seconds with every leg's mid in [0.02, 0.98]
  4. Line error for 5m and 15m (our recorded line vs the official one), by hour of day
  5. Outcome reproduction from our own feeds vs Polymarket's outcome
"""
import io
import os
import sys

import numpy as np
import pandas as pd

from recorder import config
from recorder.reader import chainlink_at, fmt_ts, load
from recorder.store import now_ms

HOUR, DAY = 3_600_000, 86_400_000
BAND = (0.02, 0.98)
pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 500)


def section(title):
    print("\n" + "=" * 90 + f"\n{title}\n" + "=" * 90)


# ---------------------------------------------------------------- 1. gaps
def gaps(start, end, chainlink, binance):
    section("1. Gaps per stream")
    health = load("health", start, end)
    logged = health[health.kind == "gap"]
    if logged.empty:
        print("Logged gaps: none")
    else:
        print("Logged gaps (from last message before to first message after):")
        print(logged.groupby("stream").value.agg(gaps="count", total_s="sum", longest_s="max").round(1).to_string())
        for _, g in logged.sort_values("start_ts").iterrows():
            print(f"  {g.stream:<20} {fmt_ts(g.start_ts)} -> {fmt_ts(g.end_ts)}  {g.value:8.1f} s  {g.detail}")
    for kind in ("start", "disconnect", "stale", "resync", "task_crash", "write_failed", "error"):
        n = (health.kind == kind).sum()
        if n:
            print(f"{kind} events: {n}")

    counts = health[health.kind == "count"]
    if not counts.empty:
        per_min = counts.pivot_table(index="start_ts", columns="stream", values="value", aggfunc="sum")
        minutes = pd.RangeIndex(start, end, 60_000)
        per_min = per_min.reindex(minutes).fillna(0)
        main = [c for c in ("books", "chainlink_twap60", "chainlink_spot", "binance") if c in per_min]
        print(f"\nMinutes of the period ({len(minutes)}) with no messages at all:")
        print((per_min[main] == 0).sum().to_string())

    seconds = pd.RangeIndex(start, end, 1000)
    print(f"\nSeconds of the period ({len(seconds)}) with no value recorded, live or backfilled:")
    for name, have in (("chainlink twap60", chainlink.src_ts[chainlink.stream == "twap60"]),
                       ("chainlink spot", chainlink.src_ts[chainlink.stream == "spot"]),
                       ("binance 1s candle", binance.open_ts[binance.closed])):
        missing = len(seconds.difference(pd.Index(have.unique())))
        print(f"  {name:<18} {missing:>6}  ({missing / len(seconds):.2%})")
    backfilled = chainlink[chainlink.backfill].groupby("stream").size()
    print(f"  of which filled by backfill rather than live: chainlink {backfilled.to_dict()}, "
          f"binance {int(binance.backfill.sum())}")
    ntp = health[health.kind == "ntp_offset_ms"].value
    if len(ntp):
        print(f"\nClock offset vs NTP in ms (positive = our clock behind): mean {ntp.mean():.1f}, "
              f"min {ntp.min():.1f}, max {ntp.max():.1f}")


# ---------------------------------------------------------------- books reduced to one row per token per second
def book_seconds(start, end, tokens):
    """Per token and second: first and last update time, and the last best bid / ask of that second."""
    parts = []
    cols = ["recv_ts", "src_ts", "asset_id", "bid_px_1", "ask_px_1"]
    for h in range(start - HOUR, end + HOUR, HOUR):          # books are stored by receive hour
        df = load("books", h, h + HOUR, columns=cols, filters=[("asset_id", "in", tokens)])
        if df.empty:
            continue
        df = df.sort_values("src_ts", kind="stable")
        df["sec"] = df.src_ts // 1000
        last = df.drop_duplicates(["asset_id", "sec"], keep="last")[["asset_id", "sec", "src_ts", "bid_px_1", "ask_px_1"]]
        first = df.drop_duplicates(["asset_id", "sec"], keep="first")[["asset_id", "sec", "src_ts"]]
        parts.append(last.rename(columns={"src_ts": "last_ts"}).merge(
            first.rename(columns={"src_ts": "first_ts"}), on=["asset_id", "sec"]))
    if not parts:
        return pd.DataFrame(columns=["asset_id", "sec", "last_ts", "bid_px_1", "ask_px_1", "first_ts"])
    out = pd.concat(parts).sort_values("last_ts", kind="stable")
    firsts = out.groupby(["asset_id", "sec"]).first_ts.min()
    out = out.drop_duplicates(["asset_id", "sec"], keep="last").drop(columns="first_ts")
    return out.join(firsts, on=["asset_id", "sec"])


def mid_per_second(token_rows, start_sec, end_sec):
    """Mid known at each whole second in [start_sec, end_sec): from the last quote strictly before it."""
    quotes = token_rows.set_index("sec")[["bid_px_1", "ask_px_1"]].sort_index()
    quotes = quotes.fillna(-1.0)                       # an empty side must not be filled over by an older quote
    known = quotes.reindex(range(start_sec - 1, end_sec - 1), method="ffill")   # last second bucket before each grid second
    known.index = known.index + 1
    known = known.where(known >= 0)
    return (known.bid_px_1 + known.ask_px_1) / 2


# ---------------------------------------------------------------- 2. coverage
def coverage(markets, secs):
    section("2. Book coverage per contract window (Up token)")
    by_token = dict(tuple(secs.groupby("asset_id")))
    rows = []
    for _, m in markets.iterrows():
        t = by_token.get(m.token_up)
        row = {"slug": m.slug, "length": m.length, "open": fmt_ts(m.open_ts)[11:], "updates_secs": 0,
               "quote_at_open": False, "first_after_open_s": np.nan, "last_before_close_s": np.nan}
        if t is not None:
            inside = t[(t.last_ts >= m.open_ts) & (t.first_ts <= m.close_ts)]
            row["updates_secs"] = len(inside)
            row["quote_at_open"] = bool((t.first_ts <= m.open_ts).any())
            if len(inside):
                row["first_after_open_s"] = (inside.first_ts[inside.first_ts >= m.open_ts].min() - m.open_ts) / 1000
                row["last_before_close_s"] = (m.close_ts - inside.last_ts[inside.last_ts <= m.close_ts].max()) / 1000
        rows.append(row)
    cov = pd.DataFrame(rows)
    if cov.empty:
        print("no markets")
        return cov
    summary = cov.groupby("length").agg(
        windows=("slug", "count"), no_quotes=("updates_secs", lambda s: (s == 0).sum()),
        quote_at_open=("quote_at_open", "sum"),
        first_after_open_median_s=("first_after_open_s", "median"), first_after_open_max_s=("first_after_open_s", "max"),
        last_before_close_median_s=("last_before_close_s", "median"), last_before_close_max_s=("last_before_close_s", "max"))
    print(summary.round(3).to_string())
    worst = cov[(cov.updates_secs == 0) | ~cov.quote_at_open | (cov.first_after_open_s > 5) | (cov.last_before_close_s > 5)]
    print(f"\nWindows with no quote at open, or more than 5 s without updates at either end: {len(worst)}")
    if len(worst):
        print(worst.head(40).to_string(index=False))
    return cov


# ---------------------------------------------------------------- 3. events
def events(markets, secs, cov):
    section("3. Events (an expiry shared by 2 or more recorded legs)")
    recorded = set(cov.slug[cov.updates_secs > 0])
    by_token = dict(tuple(secs.groupby("asset_id")))
    rows = []
    for close_ts, legs in markets[markets.slug.isin(recorded)].groupby("close_ts"):
        if len(legs) < 2:
            continue
        start_sec, end_sec = int(legs.open_ts.max() // 1000), int(close_ts // 1000)   # all legs live together
        mids = pd.DataFrame({m.length: mid_per_second(by_token[m.token_up], start_sec, end_sec) for _, m in legs.iterrows()})
        in_band = ((mids >= BAND[0]) & (mids <= BAND[1])).all(axis=1)
        rows.append({"expiry": fmt_ts(close_ts), "legs": len(legs), "seconds": len(mids),
                     "all_quoted": int(mids.notna().all(axis=1).sum()), "all_in_band": int(in_band.sum())})
    ev = pd.DataFrame(rows)
    if ev.empty:
        print("no events")
        return
    summary = ev.groupby("legs").agg(expiries=("expiry", "count"), seconds=("seconds", "sum"),
                                     all_quoted=("all_quoted", "sum"), all_in_band=("all_in_band", "sum"))
    summary["in_band_share"] = (summary.all_in_band / summary.seconds).round(3)
    print("Seconds are counted over the stretch where all legs are live (the last 5 minutes before expiry).")
    print(f"all_in_band = seconds where every leg's mid is in [{BAND[0]}, {BAND[1]}].\n")
    print(summary.to_string())
    print(f"\nExpected per full day: 24 expiries with 3 legs, 72 with 2 legs. Found {int((ev.legs == 3).sum())} and {int((ev.legs == 2).sum())}.")


# ---------------------------------------------------------------- 4 + 5. lines and outcomes
def our_values(m, chainlink, binance_by_open):
    """(line, close) for one market read from our own recorded feeds, following each market's rules."""
    if m.length in ("5m", "15m"):        # Chainlink 60s TWAP stamped exactly at open and at close
        return chainlink_at(chainlink, "twap60", m.open_ts), chainlink_at(chainlink, "twap60", m.close_ts)
    # 1h: Binance 1h candle = first trade of the hour (open) and last trade of the hour (close)
    hour = binance_by_open.loc[m.open_ts:m.close_ts - 1000]
    if len(hour) < 3600:                 # a missing second could hide the first or last trade
        return None, None
    traded = hour[hour.trades > 0]
    if traded.empty:
        return None, None
    return float(traded.iloc[0].open), float(hour.iloc[-1].close)


def lines_and_outcomes(markets, resolutions, chainlink, binance):
    binance_by_open = binance[binance.closed].drop_duplicates("open_ts").set_index("open_ts").sort_index()
    res = resolutions.drop_duplicates("slug", keep="last").set_index("slug")
    rows = []
    for _, m in markets.iterrows():
        line, close = our_values(m, chainlink, binance_by_open)
        r = res.loc[m.slug] if m.slug in res.index else None
        rows.append({
            "slug": m.slug, "length": m.length, "hour": pd.Timestamp(m.open_ts, unit="ms").hour,
            "our_line": line, "our_close": close,
            "official_line": None if r is None else r.price_to_beat,
            "official_close": None if r is None else r.final_price,
            "official_outcome": None if r is None else r.outcome,
            "our_outcome": None if line is None or close is None else ("Up" if close >= line else "Down")})
    t = pd.DataFrame(rows)
    for c in ("our_line", "our_close", "official_line", "official_close"):
        t[c] = pd.to_numeric(t[c], errors="coerce")
    t["line_err"] = t.our_line - t.official_line
    t["close_err"] = t.our_close - t.official_close

    section("4. Line error for 5m and 15m: our recorded line minus the official line, by hour of day (UTC)")
    short = t[t.length.isin(["5m", "15m"])]
    if short.empty:
        print("no 5m/15m windows")
    else:
        table = short.groupby(["length", "hour"]).agg(
            windows=("slug", "count"), our_line_missing=("our_line", lambda s: s.isna().sum()),
            official_missing=("official_line", lambda s: s.isna().sum()),
            compared=("line_err", "count"), mean_abs_err=("line_err", lambda s: s.abs().mean()),
            max_abs_err=("line_err", lambda s: s.abs().max()))
        print(table.round(6).to_string())
        for length in ("5m", "15m"):
            e = short[short.length == length].line_err.dropna().abs()
            print(f"\n{length} overall: compared {len(e)}, mean |error| ${e.mean():.6f}, max |error| ${e.max():.6f}")
        e1h = t[t.length == "1h"].line_err.dropna().abs()
        print(f"1h (Binance candle open) for reference: compared {len(e1h)}, max |error| ${e1h.max() if len(e1h) else float('nan'):.6f}")

    section("5. Outcome reproduction: our feeds vs Polymarket")
    summary = t.groupby("length").agg(
        windows=("slug", "count"),
        no_official=("official_outcome", lambda s: s.isna().sum()),
        ours_missing=("our_outcome", lambda s: s.isna().sum()))
    both = t.dropna(subset=["official_outcome", "our_outcome"])
    summary["compared"] = both.groupby("length").size()
    summary["match"] = both[both.official_outcome == both.our_outcome].groupby("length").size()
    summary = summary.fillna(0).astype(int)
    summary["mismatch"] = summary.compared - summary.match
    print(summary.to_string())
    bad = both[both.official_outcome != both.our_outcome]
    if len(bad):
        print("\nMISMATCHES (each one is a bug in our reading of the rules, or bad data):")
        print(bad[["slug", "our_line", "official_line", "our_close", "official_close", "our_outcome", "official_outcome"]].to_string(index=False))
    else:
        print("\nNo mismatches.")
    missing = t[t.our_outcome.isna()]
    if len(missing):
        print(f"\nWindows where our feeds lack the open or close value: {len(missing)} (first 20)")
        print(missing[["slug", "our_line", "our_close"]].head(20).to_string(index=False))


# ---------------------------------------------------------------- main
def run(day):
    start = int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)
    end = min(start + DAY, (now_ms() - 60_000) // 60_000 * 60_000)
    print(f"Daily check for {day} UTC   (data: {config.DATA_DIR}, run at {fmt_ts(now_ms())} UTC)")
    if end < start + DAY:
        print(f"The day is not over: checking {fmt_ts(start)} to {fmt_ts(end)} only.")

    markets = load("markets", start - 2 * DAY, end).drop_duplicates("slug")
    markets = markets[(markets.open_ts >= start) & (markets.close_ts <= end)].sort_values(["close_ts", "length"])
    print(f"Markets fully inside the day: {markets.groupby('length').size().to_dict()}  (a full day is 5m 288, 15m 96, 1h 24)")
    chainlink = load("chainlink", start - 120_000, end + 10 * 60_000)
    binance = load("binance", start - 120_000, end + 10 * 60_000)
    resolutions = load("resolutions", start, min(end + 2 * DAY, now_ms()))

    gaps(start, end, chainlink, binance)
    secs = book_seconds(start, end, list(markets.token_up))
    cov = coverage(markets, secs)
    if not cov.empty:
        events(markets, secs, cov)
        lines_and_outcomes(markets, resolutions, chainlink, binance)


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    buffer = io.StringIO()
    stdout = sys.stdout

    class Tee:
        def write(self, text):
            stdout.write(text)
            buffer.write(text)

        def flush(self):
            stdout.flush()

    sys.stdout = Tee()
    try:
        run(day)
    finally:
        sys.stdout = stdout
    folder = os.path.join(config.DATA_DIR, "reports")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"daily_{day}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(buffer.getvalue())
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
