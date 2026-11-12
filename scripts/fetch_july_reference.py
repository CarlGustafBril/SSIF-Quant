"""Public reference data the shared July dataset lacks: official lines/outcomes and Binance 1-second closes.

    python scripts/fetch_july_reference.py

The shared order-book polls (2026-07-02 to 07-22) carry no official line for 5m/15m contracts and
no outcomes, and the shared Binance 1-second file ends on 07-01. Both are public:

  * Gamma API: `eventMetadata.priceToBeat` / `finalPrice` for every closed contract, by slug.
  * data.binance.vision: daily zip files of BTCUSDT 1-second candles.

Writes to <POLYDATA_DIR>/reference/ (never into shared/):
  gamma_lines.parquet    slug, length, open_ts, close_ts, line, final, up, settles_on
  binance_1s.parquet     open_ts (ms), open, close
Both are resumable: what is already there is kept, only the missing part is fetched.
"""
import io
import json
import os
import sys
import time
import zipfile

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recorder import config  # noqa: E402

OUT_DIR = os.path.join(config.DATA_DIR, "reference")
SHARED = os.path.join(config.DATA_DIR, "shared", "data")
GAMMA = "https://gamma-api.polymarket.com/events"
VISION = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-{day}.zip"
FIRST_DAY, LAST_DAY = "2026-07-01", "2026-07-22"
BATCH = 40
LENGTH_S = {"5m": 300, "15m": 900, "1h": 3600}


def wanted_contracts():
    """slug, length, close_ts (s) for every 5m/15m/1h contract in the July order-book polls."""
    ob = pd.read_parquet(os.path.join(SHARED, "orderbook_rest", "orderbook_rest_dedup.parquet"),
                         columns=["duration_label", "contract_slug", "expiry"])
    ob = ob[ob.duration_label.isin(LENGTH_S)].drop_duplicates("contract_slug")
    close = (pd.to_datetime(ob.expiry, utc=True) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
    out = pd.DataFrame({"slug": ob.contract_slug.values, "length": ob.duration_label.values, "close_ts": close.values})
    out = out[out.close_ts >= pd.Timestamp(FIRST_DAY, tz="UTC").timestamp()]
    # the tick extracts run past the polls on some days: add the 5m/15m contracts they imply
    extra = []
    folder = os.path.join(SHARED, "events")
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if name[:2] == "20" and name.endswith(".parquet"):
            t = int(pd.to_datetime(name[:15], format="%Y%m%d_%H%M%S", utc=True).timestamp())
            for length, secs in (("5m", 300), ("15m", 900)):
                extra.append({"slug": f"btc-updown-{length}-{t - secs}", "length": length, "close_ts": t})
    out = pd.concat([out, pd.DataFrame(extra)]).drop_duplicates("slug")
    return out.sort_values("close_ts").reset_index(drop=True)


def fetch_lines():
    path = os.path.join(OUT_DIR, "gamma_lines.parquet")
    have = pd.read_parquet(path) if os.path.exists(path) else pd.DataFrame(columns=["slug"])
    want = wanted_contracts()
    todo = want[~want.slug.isin(have.slug)]
    print(f"lines: {len(want)} contracts wanted, {len(have)} already saved, {len(todo)} to fetch", flush=True)
    session, rows = requests.Session(), []
    session.headers["Accept-Encoding"] = "gzip"
    lengths = dict(zip(want.slug, want.length))
    for i in range(0, len(todo), BATCH):
        slugs = list(todo.slug.iloc[i:i + BATCH])
        for attempt in range(5):
            try:
                r = session.get(GAMMA, params=[("slug", s) for s in slugs] + [("limit", BATCH)], timeout=40)
                r.raise_for_status()
                events = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                print(f"  retry {attempt + 1}: {str(e)[:80]}", flush=True)
                time.sleep(3 * (attempt + 1))
        else:
            continue
        for e in events:
            meta = e.get("eventMetadata") or {}
            m = (e.get("markets") or [{}])[0]
            prices = json.loads(m.get("outcomePrices") or "[]")
            desc = (e.get("description") or "").lower()
            rows.append({
                "slug": e["slug"], "length": lengths.get(e["slug"]),
                "open_ts": int(pd.Timestamp(m["eventStartTime"]).timestamp()) if m.get("eventStartTime") else None,
                "close_ts": int(pd.Timestamp(e["endDate"]).timestamp()),
                "line": meta.get("priceToBeat"), "final": meta.get("finalPrice"),
                "up": float(prices[0]) if len(prices) == 2 and m.get("closed") else None,
                "settles_on": "binance" if "binance" in desc else "chainlink_twap" if "twap" in desc
                              else "chainlink_spot" if "chainlink" in desc else "unknown",
                "tick": m.get("orderPriceMinTickSize"),
            })
        if (i // BATCH) % 25 == 24 or i + BATCH >= len(todo):
            print(f"  {min(i + BATCH, len(todo))}/{len(todo)}", flush=True)
            if rows:
                have = pd.concat([have, pd.DataFrame(rows)], ignore_index=True).drop_duplicates("slug")
                have.to_parquet(path)
                rows = []
        time.sleep(0.15)
    print(f"lines saved: {len(have)} rows -> {path}")
    return have


def fetch_binance():
    path = os.path.join(OUT_DIR, "binance_1s.parquet")
    have = pd.read_parquet(path) if os.path.exists(path) else pd.DataFrame(columns=["open_ts", "open", "close"])
    days_have = set(pd.to_datetime(have.open_ts, unit="ms").dt.strftime("%Y-%m-%d").unique()) if len(have) else set()
    parts = [have]
    for day in pd.date_range(FIRST_DAY, LAST_DAY).strftime("%Y-%m-%d"):
        if day in days_have:
            continue
        r = requests.get(VISION.format(day=day), timeout=120)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            k = pd.read_csv(z.open(z.namelist()[0]), header=None, usecols=[0, 1, 4], names=["open_ts", "open", "close"])
        if k.open_ts.iloc[0] > 10 ** 14:              # Binance switched these files to microseconds in 2025
            k["open_ts"] //= 1000
        parts.append(k)
        print(f"  binance {day}: {len(k)} candles", flush=True)
    out = pd.concat(parts, ignore_index=True).drop_duplicates("open_ts").sort_values("open_ts")
    out.to_parquet(path)
    print(f"binance saved: {len(out)} rows -> {path}")


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    fetch_binance()
    fetch_lines()
