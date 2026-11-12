"""Phase 1 acceptance report for a recorded period.

    python -m recorder.acceptance                      # everything recorded so far
    python -m recorder.acceptance 2026-11-11T20:23 2026-11-11T23:23    # a UTC window
"""
import sys

import numpy as np
import pandas as pd

from . import config
from .reader import chainlink_at, fmt_ts, load, quotes_on_grid, size_bytes
from .store import SCHEMAS, now_ms

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)
pd.set_option("display.max_rows", 500)


def section(title):
    print("\n" + "=" * 100 + f"\n{title}\n" + "=" * 100)


def rates_gaps_sizes(start, end):
    section("1. Message rates, gaps, file sizes")
    hours = (end - start) / 3_600_000
    health = load("health", start, end)
    counts = health[(health.kind == "count") & (health.start_ts >= start) & (health.start_ts + 60_000 <= end)]
    if not counts.empty:
        per_min = counts.pivot_table(index="start_ts", columns="stream", values="value", aggfunc="sum").fillna(0)
        summary = pd.DataFrame({"total": per_min.sum(), "per_sec_mean": per_min.mean() / 60,
                                "per_min_min": per_min.min(), "per_min_max": per_min.max(),
                                "minutes_with_zero": (per_min == 0).sum()})
        print(f"Period {fmt_ts(start)} to {fmt_ts(end)} UTC ({hours:.2f} h), {len(per_min)} full minutes counted\n")
        print(summary.round(2).to_string())

    print("\nConnection events and gaps:")
    events = health[health.kind.isin(["gap", "disconnect", "stale", "task_crash", "write_failed", "resync",
                                      "start", "stop", "error", "market_missing", "hourly_missing",
                                      "backfill_failed", "resolution_incomplete", "ntp_failed"])]
    if events.empty:
        print("  none")
    for _, e in events.iterrows():
        span = f" {fmt_ts(e.start_ts)} -> {fmt_ts(e.end_ts)} ({e.value:.1f} s)" if e.kind == "gap" else ""
        print(f"  {fmt_ts(e.recv_ts)}  {e.stream:<20} {e.kind:<12}{span} {e.detail[:90]}")
    gaps = events[events.kind == "gap"]
    if len(gaps):
        print("\nGaps by stream:")
        print(gaps.groupby("stream").value.agg(gaps="count", total_s="sum", longest_s="max").round(1).to_string())
    else:
        print("\n  gaps: 0")
    lag = health[health.kind == "loop_lag_ms"].value
    if len(lag):
        print(f"\nEvent loop lag per minute: median {lag.median():.0f} ms, max {lag.max():.0f} ms")

    ntp = health[health.kind == "ntp_offset_ms"].value
    if len(ntp):
        print(f"\nClock offset vs NTP (positive = our clock behind): mean {ntp.mean():.1f} ms, "
              f"min {ntp.min():.1f}, max {ntp.max():.1f}, n={len(ntp)}")
    flush = health[health.kind == "flush"].value
    if len(flush):
        print(f"Parquet flush time: mean {flush.mean():.2f} s, max {flush.max():.2f} s")

    print("\nFile sizes:")
    total = 0
    for table in SCHEMAS:
        size = size_bytes(table, start, end)
        total += size
        print(f"  {table:<12} {size / 1e6:10.2f} MB   {size / 1e6 / hours:8.2f} MB/h   14 days: {size / 1e9 / hours * 336:7.2f} GB")
    print(f"  {'TOTAL':<12} {total / 1e6:10.2f} MB   {total / 1e6 / hours:8.2f} MB/h   14 days: {total / 1e9 / hours * 336:7.2f} GB")


def line_check(start, end):
    section("2. Our line (recorded Chainlink 60s TWAP at window open) vs official priceToBeat, 5m and 15m")
    res = load("resolutions", start, now_ms())
    res = res[res.length.isin(["5m", "15m"]) & (res.open_ts >= start) & (res.close_ts <= end)].drop_duplicates("slug")
    markets = load("markets", start - 2 * 3_600_000, end).drop_duplicates("slug")
    expected = markets[markets.length.isin(["5m", "15m"]) & (markets.open_ts >= start) & (markets.close_ts <= end)]
    chainlink = load("chainlink", start - 120_000, end + 120_000)
    rows = []
    for _, r in res.sort_values(["open_ts", "length"]).iterrows():
        ours_open = chainlink_at(chainlink, "twap60", r.open_ts)
        ours_close = chainlink_at(chainlink, "twap60", r.close_ts)
        our_outcome = None if None in (ours_open, ours_close) else ("Up" if ours_close >= ours_open else "Down")
        rows.append({"open_utc": fmt_ts(r.open_ts)[11:], "len": r.length, "our_line": ours_open,
                     "official_line": r.price_to_beat,
                     "line_diff": None if ours_open is None or pd.isna(r.price_to_beat) else ours_open - r.price_to_beat,
                     "our_close": ours_close, "official_close": r.final_price,
                     "close_diff": None if ours_close is None or pd.isna(r.final_price) else ours_close - r.final_price,
                     "official": r.outcome, "ours": our_outcome})
    table = pd.DataFrame(rows)
    print(f"Windows fully inside the period: {len(expected)} listed, {len(res)} with a resolution row so far\n")
    if table.empty:
        return
    print(table.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    for length in ("5m", "15m"):
        t = table[table.len == length]
        d = t.line_diff.dropna().astype(float).abs()
        c = t.close_diff.dropna().astype(float).abs()
        both = t.dropna(subset=["official", "ours"])
        print(f"\n{length}: windows {len(t)} | line compared {len(d)}, mean |diff| ${d.mean():.6f}, max |diff| ${d.max():.6f}"
              f" | close compared {len(c)}, max |diff| ${c.max():.6f}"
              f" | our line missing {t.our_line.isna().sum()} | official line missing {t.official_line.isna().sum()}"
              f" | outcome matches {(both.official == both.ours).sum()}/{len(both)}")


def hourly_events(start, end, want=2):
    section("3. Hourly events: 5m, 15m and 1h books that expire together, on a 1-second grid")
    markets = load("markets", start - 2 * 3_600_000, end).drop_duplicates("slug")
    hourly = markets[(markets.length == "1h") & (markets.open_ts >= start) & (markets.close_ts <= end)].sort_values("close_ts")
    print(f"Hourly contracts recorded from open to close in the period: {len(hourly)}")
    for _, h in hourly.tail(want).iterrows():
        expiry = int(h.close_ts)
        legs = markets[(markets.close_ts == expiry)].set_index("length")
        if not {"5m", "15m", "1h"} <= set(legs.index):
            print(f"\nExpiry {fmt_ts(expiry)}: missing a leg, have {sorted(legs.index)}")
            continue
        print(f"\nExpiry {fmt_ts(expiry)} UTC   ({h.slug})")
        grid = np.arange(int(h.open_ts), expiry + 1, 1000, dtype="int64")
        out = pd.DataFrame({"ts": grid})
        cols = ["recv_ts", "src_ts", "asset_id", "bid_px_1", "bid_sz_1", "ask_px_1", "ask_sz_1"]
        for length in ("5m", "15m", "1h"):
            leg = legs.loc[length]
            books = load("books", int(leg.open_ts) - 400_000, expiry + 5_000, columns=cols,
                         filters=[("asset_id", "==", leg.token_up)])
            live = (books.src_ts >= leg.open_ts) & (books.src_ts <= expiry)
            first, last = books.src_ts[live].min(), books.src_ts[live].max()
            print(f"  {length:>3}: {len(books):>9,} Up-token updates | first quote {(first - leg.open_ts) / 1000:+.3f} s after open,"
                  f" last {(last - expiry) / 1000:+.3f} s vs close")
            q = quotes_on_grid(books, grid)
            in_window = (grid >= leg.open_ts)
            for src, dst in (("bid_px_1", "bid"), ("ask_px_1", "ask"), ("mid", "mid"), ("age_ms", "age_ms")):
                out[f"{dst}_{length}"] = q[src].where(in_window).values
        last5 = out[out.ts >= expiry - 300_000]
        mids = last5[["mid_5m", "mid_15m", "mid_1h"]]
        all_quoted = mids.notna().all(axis=1)
        in_band = ((mids >= 0.02) & (mids <= 0.98)).all(axis=1)
        print(f"  1h window: {len(out)} grid seconds, 1h leg quoted on {out.mid_1h.notna().sum()}")
        print(f"  final 5 minutes (all three legs live): {len(last5)} grid seconds, all three quoted on {all_quoted.sum()},"
              f" every mid in [0.02, 0.98] on {in_band.sum()}")
        print(f"  quote age in final 5 min, median / max ms: " + ", ".join(
            f"{l} {last5[f'age_ms_{l}'].median():.0f} / {last5[f'age_ms_{l}'].max():.0f}" for l in ("5m", "15m", "1h")))
        sample = last5[(last5.ts - expiry) % 20_000 == 0].copy()
        sample.insert(0, "utc", sample.ts.map(lambda t: fmt_ts(t)[11:]))
        show = ["utc"] + [f"{c}_{l}" for l in ("5m", "15m", "1h") for c in ("bid", "ask")] + [f"age_ms_{l}" for l in ("5m", "15m", "1h")]
        print("\n  Every 20th second of the final 5 minutes (Up token best bid / ask):")
        print("  " + sample[show].to_string(index=False).replace("\n", "\n  "))


def main():
    health_all = load("health", 0, now_ms(), columns=["recv_ts"]) if len(sys.argv) < 3 else None
    if len(sys.argv) >= 3:
        start = int(pd.Timestamp(sys.argv[1], tz="UTC").timestamp() * 1000)
        end = int(pd.Timestamp(sys.argv[2], tz="UTC").timestamp() * 1000)
    else:
        start, end = int(health_all.recv_ts.min()), now_ms()
    print(f"Data folder: {config.DATA_DIR}")
    rates_gaps_sizes(start, end)
    line_check(start, end)
    hourly_events(start, end)


if __name__ == "__main__":
    main()
