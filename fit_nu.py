"""Offline tail fit: Student-t degrees of freedom (nu) per horizon, from Binance 1-minute candles.

    python fit_nu.py            # last 90 complete UTC days
    python fit_nu.py 30         # another number of days

For each horizon (5, 15, 60 minutes) the log return over every clock-aligned window is divided by
the realised volatility of the 60 minutes before it (scaled to the horizon). scipy fits a
Student-t with centre 0 to those ratios. We keep nu and discard the scale.

Output: nu.json next to this file, and in <POLYDATA_DIR>/analysis/ the QQ points as CSV
and as QQ plots (SVG files, open in a browser).
"""
import io
import json
import os
import sys
import zipfile

import numpy as np
import pandas as pd
import requests
from scipy import stats

import svgplot
from recorder import config

ARCHIVE = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{day}.zip"
HORIZONS_MIN = (5, 15, 60)
RV_MINUTES = 60
NU_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nu.json")
OUT_DIR = os.path.join(config.DATA_DIR, "analysis")
CACHE_DIR = os.path.join(config.DATA_DIR, "binance_archive")


def download_day(day):
    """One day of 1-minute candles as a DataFrame (open_ts in ms, close). Cached on disk as CSV."""
    path = os.path.join(CACHE_DIR, f"BTCUSDT-1m-{day}.csv")
    if not os.path.exists(path):
        r = requests.get(ARCHIVE.format(day=day), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(z.namelist()[0])
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(path + ".tmp", path)
    df = pd.read_csv(path, header=None, usecols=[0, 4], names=["open_ts", "close"])
    df = df[pd.to_numeric(df.open_ts, errors="coerce").notna()].astype({"open_ts": "int64", "close": "float64"})
    if df.open_ts.iloc[0] > 10**14:          # the archive switched from milliseconds to microseconds in 2025
        df["open_ts"] //= 1000
    return df


def load_candles(days):
    """Minute closes for the last `days` complete UTC days, on a gap-free minute index (NaN where missing)."""
    today = pd.Timestamp.now(tz="UTC").normalize()
    parts, missing = [], []
    for back in range(days + 1, 0, -1):      # the archive publishes a day late, so allow one spare
        day = (today - pd.Timedelta(days=back)).strftime("%Y-%m-%d")
        df = download_day(day)
        if df is None:
            missing.append(day)
        else:
            parts.append(df)
    candles = pd.concat(parts).drop_duplicates("open_ts").set_index("open_ts").close.sort_index()
    candles = candles.iloc[-days * 1440:]
    full = candles.reindex(range(candles.index[0], candles.index[-1] + 60_000, 60_000))
    print(f"Candles: {len(candles):,} minutes from {pd.Timestamp(full.index[0], unit='ms')} to "
          f"{pd.Timestamp(full.index[-1], unit='ms')} UTC; missing minutes {int(full.isna().sum())}; "
          f"days not in archive {missing or 'none'}")
    return full


def standardised_returns(close, horizon_min):
    """z = (log return over each clock-aligned window) / (realised vol of the previous 60 min, scaled).

    close: minute closes indexed by candle open time in ms. A candle's close is the price one
    minute after its open time, so the price AT time t is the close of the candle that opened at t - 1 min.
    """
    logp = np.log(close)
    r1 = logp.diff()                                              # 1-minute log returns
    # realised vol per minute over the 60 minutes ending at each time (zero mean assumed)
    rv = np.sqrt((r1 ** 2).rolling(RV_MINUTES, min_periods=RV_MINUTES).mean())
    step = horizon_min * 60_000
    price_time = logp.index + 60_000                              # the time each close refers to
    is_start = price_time % step == 0
    start_pos = np.flatnonzero(is_start)
    start_pos = start_pos[start_pos + horizon_min < len(logp)]
    ret = logp.values[start_pos + horizon_min] - logp.values[start_pos]
    vol = rv.values[start_pos] * np.sqrt(horizon_min)
    z = ret / vol
    return z[np.isfinite(z)]


def qq_points(z, nu, scale):
    """Sorted sample against the matching quantiles of the fitted t and of a normal with the sample's sd."""
    z = np.sort(z)
    probs = (np.arange(1, len(z) + 1) - 0.5) / len(z)
    return pd.DataFrame({"sample": z, "t_quantile": stats.t.ppf(probs, nu, scale=scale),
                         "normal_quantile": stats.norm.ppf(probs, scale=z.std())})


def save_plot(qq, horizon_min, nu, path):
    """QQ plot of the sample against the fitted t and against a normal."""
    qq = svgplot.thin(qq)
    svgplot.save(path, [
        {"title": f"{horizon_min}-minute returns vs Student-t, nu = {nu:.2f}", "xlabel": "t quantile",
         "ylabel": "sample quantile", "x": qq.t_quantile, "y": qq["sample"], "diagonal": True},
        {"title": f"{horizon_min}-minute returns vs normal", "xlabel": "normal quantile",
         "ylabel": "sample quantile", "x": qq.normal_quantile, "y": qq["sample"], "diagonal": True}])


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    close = load_candles(days)
    os.makedirs(OUT_DIR, exist_ok=True)
    result = {}
    print(f"\n{'horizon':>8} {'windows':>8} {'nu':>7} {'t scale':>8} {'sd of z':>8} {'|z|>4: seen':>12} {'normal':>8} {'t':>8}  flag")
    for h in HORIZONS_MIN:
        z = standardised_returns(close, h)
        nu, _, scale = stats.t.fit(z, floc=0)
        seen = int((np.abs(z) > 4 * z.std()).sum())
        exp_normal = 2 * stats.norm.sf(4) * len(z)
        exp_t = 2 * stats.t.sf(4 * z.std() / scale, nu) * len(z)
        flag = "nu > 10: weak fat-tail case" if nu > 10 else "nu < 3: variance undefined below 2, unstable" if nu < 3 else ""
        print(f"{h:>6} m {len(z):>8,} {nu:>7.2f} {scale:>8.3f} {z.std():>8.3f} {seen:>12} {exp_normal:>8.1f} {exp_t:>8.1f}  {flag}")
        result[str(h)] = round(float(nu), 3)
        qq = qq_points(z, nu, scale)
        qq.to_csv(os.path.join(OUT_DIR, f"qq_{h}m.csv"), index=False)
        save_plot(qq, h, nu, os.path.join(OUT_DIR, f"qq_{h}m.svg"))
    print("\n'|z|>4' counts returns beyond 4 standard deviations: seen, and expected under each distribution.")
    with open(NU_FILE, "w") as f:
        json.dump({"nu_by_horizon_minutes": result, "days": days,
                   "fitted_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M")}, f, indent=2)
    print(f"Saved {NU_FILE}")
    print(f"QQ plots (qq_<horizon>m.svg) and their points (CSV) saved in {OUT_DIR}")

if __name__ == "__main__":
    main()
