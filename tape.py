"""The same rules on Polymarket's public trade tape, for periods nobody recorded the order book.

    python tape.py validate            # July: quotes rebuilt from trades against the recorded book
    python tape.py fetch 2026-10-13 2026-11-11     # lines, outcomes, Binance candles and trades for those UTC days
    python tape.py build now           # per-second signal table for a fetched period ("now" or "autumn", see PERIODS)
    python tape.py check               # today's rule: the tape version against our own recorder, same expiries

Why: our own recording under today's settlement rule (60-second average, since mid-August 2026)
is a day old. The exchange publishes every trade of every contract (data-api.polymarket.com/v2),
with price, side and the second it happened. A trade is an executed quote: a taker buying Up paid
the ask, a taker selling Up received the bid (and buying Down is selling Up at 1 - price). So the
best bid and ask can be rebuilt, roughly, from the last trades on each side.

How rough is measured, not assumed: the July tick files hold both the book and the trades, so
`validate` runs the rules on trade-built quotes and compares with the book-built result, and
prices every trade the tape version picks at the real book.

Quotes from trades, at second t (a trade stamped second s counts from s + 1):
  ask = price of the last trade that lifted the Up ask, bid = the last that hit the Up bid, each at
  most MAX_TRADE_AGE_S old. If only one side is fresh, or the two are crossed or more than
  MAX_SPREAD apart (the price moved between them), the fresher trade sets its side and the other
  side is one tick away.

Everything else is as in july.py: official lines and outcomes, Binance 1-second candles, and
Chainlink taken as Binance minus the gap known at each 5-minute boundary. Under today's rule the
line is a 60-second average, so the gap is measured against Binance's own 60-second average.
"""
import glob
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import numpy as np
import pandas as pd

import july
import votes
from recorder import config

# The same contracts exist for other coins. POLY_ASSET=eth (sol, xrp) in the environment switches every
# path and slug below; an environment variable, so that worker processes see it too. Default: btc.
ASSETS = {"btc": ("bitcoin", "BTCUSDT"), "eth": ("ethereum", "ETHUSDT"), "sol": ("solana", "SOLUSDT"), "xrp": ("xrp", "XRPUSDT")}
ASSET = os.environ.get("POLY_ASSET", "btc").lower()
LONG_NAME, SYMBOL = ASSETS[ASSET]
SUFFIX = "" if ASSET == "btc" else f"_{ASSET}"
REFERENCE = os.path.join(config.DATA_DIR, "reference")
TAPE_DIR = os.path.join(REFERENCE, "tape" + SUFFIX)
CANDLES = os.path.join(REFERENCE, "binance_1s.parquet" if ASSET == "btc" else f"binance_1s_{SYMBOL}.parquet")
OUT_DIR = os.path.join(config.DATA_DIR, "analysis")
TRADES_URL = "https://data-api.polymarket.com/v2/trades"
MAX_TRADE_AGE_S = 15
MAX_SPREAD = 0.03
TICK = 0.01
LENGTH_S = july.LENGTH_S
BINANCE_AGE_MS = 1000
CHAINLINK_AGE_MS = 3000
TRADE_QUOTE_EXTRA_AGE_MS = 1000        # a trade-built quote is at least a second old; widen the error bar for it
API_LAG_S = 4                          # the public tape stamps a trade about 4 s after it matched (see lag_scan)


# ============================================================================ quotes from trades
def up_trades(trades, lag_s=0):
    """Trades of one contract as (recv_ts in ms, Up price, lift). lift: the taker bought Up exposure.

    trades: columns ts (whole seconds), price, side ("BUY"/"SELL"), outcome_index (0 Up, 1 Down).
    lag_s: how long after the match the time stamp was made. The July files carry the exchange's
    own match time (0). The public tape's stamp is about 4 s late: against our recorded book the
    rebuilt quotes fit best when moved 4 s earlier (mean error 1.5 c instead of 3.7 c on the 5m).
    """
    up = trades.outcome_index.values == 0
    buy = trades.side.str.upper().values == "BUY"
    out = pd.DataFrame({"recv_ts": (trades.ts.values.astype("int64") + 1 - lag_s) * 1000,
                        "px": np.where(up, trades.price.values, 1 - trades.price.values),
                        "lift": up == buy})
    return out.sort_values("recv_ts", kind="stable")


def quotes_from_trades(trades, grid, lag_s=0):
    """Best bid and ask of the Up token at each grid time, rebuilt from the trades. NaN when no trade is fresh."""
    t = up_trades(trades, lag_s)
    sides = {}
    for name, part in (("ask", t[t.lift]), ("bid", t[~t.lift])):
        got = votes.asof(grid, part, ["px"]) if len(part) else None
        px = got.px.values if got is not None else np.full(len(grid), np.nan)
        age = (got.ts - got.recv_ts).values if got is not None else np.full(len(grid), np.nan)
        fresh = age <= MAX_TRADE_AGE_S * 1000
        sides[name] = (np.where(fresh, px, np.nan), np.where(fresh, age, np.inf))
    (ask, ask_age), (bid, bid_age) = sides["ask"], sides["bid"]
    both = np.isfinite(ask) & np.isfinite(bid)
    bad = both & ((bid >= ask) | (ask - bid > MAX_SPREAD))
    use_ask = (np.isfinite(ask) & ~np.isfinite(bid)) | (bad & (ask_age <= bid_age))
    use_bid = (np.isfinite(bid) & ~np.isfinite(ask)) | (bad & (bid_age < ask_age))
    bid = np.where(use_ask, ask - TICK, bid)
    ask = np.where(use_bid, bid + TICK, ask)
    age = np.minimum(ask_age, bid_age)
    return np.clip(bid, 0.0, 1.0), np.clip(ask, 0.0, 1.0), np.where(np.isfinite(age), age, np.nan)


# ============================================================================ reference prices
def chainlink_stand_in(grid, ref, w):
    """Chainlink spot at each grid time: Binance minus the gap at the last 5-minute boundary.

    ref["gap"] holds the gap at each boundary, measured against Binance's average over the last w
    seconds when the line is an average (w > 0), else against the candle that closed there.
    """
    binance = ref["candles"].close.reindex(grid - 2000).values                 # closed at t - 1 s
    gap = votes.asof(grid, ref["gap"], ["gap"])
    ok = (gap.ts - gap.recv_ts).values <= 600_000
    return binance, np.where(ok, binance - gap.gap.values, np.nan)


def averaged_reference(grid, spot, close_ts, w, ref):
    """Inside the last w seconds part of the settlement average is fixed: blend it with spot, as votes does."""
    out = spot.copy()
    inside = np.flatnonzero(grid > close_ts - w * 1000)
    if not len(inside):
        return out
    seconds = np.arange(close_ts - w * 1000 + 1000, close_ts + 1000, 1000, dtype="int64")
    _, path = chainlink_stand_in(seconds, ref, w)                              # what we knew at each second of the window
    for n in inside:
        seen = path[seconds <= grid[n]]
        seen = seen[np.isfinite(seen)]
        tau = (close_ts - grid[n]) / 1000
        if len(seen):
            out[n] = ((w - tau) * seen.mean() + tau * out[n]) / w
    return out


def event_table(expiry, legs, ref, windows, nu_by_horizon=None, lag_s=0):
    """One row per second, in the layout of votes.event_table, with quotes rebuilt from trades.

    legs: {name: trades of that contract}. ref: candles, gap, line(name, open_ts in ms).
    """
    nu_by_horizon = nu_by_horizon or votes.load_nu()
    names = [n for n in ("5m", "15m", "1h") if n in legs and len(legs[n])]
    if len(names) < 2:
        return None
    opens = {n: expiry - LENGTH_S[n] * 1000 for n in names}
    grid = np.arange(sorted(opens.values())[-2], expiry, 1000, dtype="int64")
    candles = ref["candles"]
    table = pd.DataFrame({"expiry": expiry, "ts": grid, "tau": (expiry - grid) / 1000})
    table["sd"] = votes.realised_sd(grid, candles)
    table["sd_fast"] = votes.realised_sd(grid, candles, returns=30, step_s=10)
    w_chain = max(windows["5m"], windows["15m"])
    binance, chainlink = chainlink_stand_in(grid, ref, w_chain)
    for n in names:
        bid, ask, age = quotes_from_trades(legs[n], grid, lag_s)
        live = grid >= opens[n]
        if n == "1h":
            spot, feed_age, line = binance, BINANCE_AGE_MS, candles.open.get(opens[n], np.nan)
        else:
            spot, feed_age, line = chainlink, CHAINLINK_AGE_MS, ref["line"](n, opens[n])
            if windows[n]:
                spot = averaged_reference(grid, spot, expiry, windows[n], ref)
        table[f"bid_{n}"] = np.where(live, bid, np.nan)
        table[f"ask_{n}"] = np.where(live, ask, np.nan)
        table[f"quote_age_{n}"] = np.where(live, age, np.nan)
        table[f"spot_{n}"] = spot
        table[f"feed_age_{n}"] = float(feed_age + TRADE_QUOTE_EXTRA_AGE_MS)
        table[f"line_{n}"] = line
        table[f"tick_{n}"] = TICK
    decided = votes.decide_rows(table, names, nu_by_horizon, windows=windows)
    result = pd.concat([table, pd.DataFrame(decided)], axis=1)
    for n in names:
        for col in (f"x_{n}", f"v_{n}", f"sv_{n}"):
            if col not in result:
                result[col] = np.nan
    return result


def finish(tables, path):
    signals = pd.concat([t for t in tables if t is not None], ignore_index=True)
    for col in signals.columns:
        if col.split("_z")[0] in ("kind", "outlier", "lo", "hi", "side"):
            signals[col] = signals[col].astype("string")
    os.makedirs(OUT_DIR, exist_ok=True)
    signals.to_parquet(path)
    return signals


# ============================================================================ July: tape against book
def _july_event(path):
    """The tape version of one July tick file: its trades, turned into the public tape's format."""
    ref = july.reference()
    expiry = july.expiry_of(path) * 1000
    rows = pd.read_parquet(path, columns=["rowtype", "leg", "token", "price", "side", "ex_ms"])
    up_token = rows[rows.rowtype == "book"].groupby("leg").token.first()
    trades = rows[(rows.rowtype == "trade") & rows.leg.isin(up_token.index)]
    legs = {}
    for n, t in trades.groupby("leg"):
        legs[n] = pd.DataFrame({"ts": (t.ex_ms // 1000).astype("int64"), "price": t.price, "side": t.side,
                                "outcome_index": (t.token != up_token[n]).astype(int)})
    july_ref = {"candles": ref["candles"], "gap": ref["gap"],
                "line": lambda n, open_ms: ref["chainlink_at"].get(open_ms // 1000, np.nan)}
    return event_table(expiry, legs, july_ref, july.WINDOWS)


def july_tape_path():
    return os.path.join(OUT_DIR, "tape_july.parquet")


def build_july(workers=6):
    files = july.event_files("tick") + july.event_files("holdout")
    print(f"rebuilding {len(files)} July events from their trades ...", flush=True)
    with ProcessPoolExecutor(workers) as pool:
        tables = list(pool.map(_july_event, files, chunksize=4))
    return finish(tables, july_tape_path())


def validate():
    """Do trade-built quotes give the same answer as the book on July, where we have both?"""
    import backtest
    book = pd.concat([july.load("tick"), july.load("holdout")]).sort_values(["expiry", "ts"]).reset_index(drop=True)
    tape = (pd.read_parquet(july_tape_path()) if os.path.exists(july_tape_path()) else build_july())
    tape = tape.sort_values(["expiry", "ts"]).reset_index(drop=True)
    outcomes = july.outcomes()
    print(f"July, {book.expiry.nunique()} expiries with both book and trades.\n")
    both = book.merge(tape, on=["expiry", "ts"], suffixes=("", "_tape"))
    print("1. Trade-built quotes against the book (seconds where both exist, cents):")
    rows = []
    for n in ("5m", "15m", "1h"):
        d = both[[f"bid_{n}", f"ask_{n}", f"bid_{n}_tape", f"ask_{n}_tape"]]
        have_book = d[f"bid_{n}"].notna() & d[f"ask_{n}"].notna()
        d2 = d[have_book].dropna()
        mid_err = ((d2[f"bid_{n}_tape"] + d2[f"ask_{n}_tape"]) - (d2[f"bid_{n}"] + d2[f"ask_{n}"])) / 2 * 100
        rows.append({"contract": n, "seconds with a book quote": int(have_book.sum()), "also a tape quote": len(d2) / max(have_book.sum(), 1),
                     "mid error median": mid_err.median(), "typical size": mid_err.abs().median(), "95th pct": mid_err.abs().quantile(0.95),
                     "tape ask - book ask": ((d2[f"ask_{n}_tape"] - d2[f"ask_{n}"]) * 100).mean(),
                     "tape bid - book bid": ((d2[f"bid_{n}_tape"] - d2[f"bid_{n}"]) * 100).mean()})
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    print("\n2. The frozen rules on each (one trade per expiry, 4.5 c edge, held to expiry, cents per trade):")
    t_book, _ = backtest.simulate(book, outcomes, backtest.DEFAULT_THRESHOLD)
    t_tape, _ = backtest.simulate(tape, outcomes, backtest.DEFAULT_THRESHOLD)

    def line(label, pnl):
        pnl = pnl.dropna() * 100
        se = pnl.std(ddof=1) / np.sqrt(len(pnl))
        print(f"  {label:<58} {len(pnl):>4} trades, won {(pnl > 0).mean():.0%}, {pnl.mean():+.1f} c (se {se:.1f})")
    line("book quotes (the July result)", t_book.pnl)
    line("tape quotes, filled at tape prices", t_tape.pnl)
    # the same tape trades, but paid for at the real book one second after the signal
    q = book.set_index(["expiry", "ts"])
    real = []
    for t in t_tape.itertuples():
        key = (t.expiry, t.ts + backtest.DELAY_S * 1000)
        if key not in q.index:
            real.append(np.nan)
            continue
        e = q.loc[key]
        price = e[f"ask_{t.lo}"] - e[f"bid_{t.hi}"] if t.side == "buy" else e[f"bid_{t.lo}"] - e[f"ask_{t.hi}"]
        real.append(t.payoff - price if t.side == "buy" else price - t.payoff)
    t_tape = t_tape.assign(pnl_at_book=real)
    line("tape quotes, the same trades filled at the real book", t_tape.pnl_at_book)
    filled = t_tape.dropna(subset=["pnl_at_book"])
    print(f"  fill optimism of the tape: {(filled.pnl - filled.pnl_at_book).mean() * 100:+.1f} c per trade "
          f"(tape P&L minus real-book P&L on the same {len(filled)} trades)")
    same = t_tape.merge(t_book, on="expiry", suffixes=("_tape", "_book"))
    agree = (same.side_tape == same.side_book) & (same.lo_tape == same.lo_book) & (same.hi_tape == same.hi_book)
    print(f"  expiries traded by both: {len(same)} (of {len(t_tape)} tape, {len(t_book)} book), same legs and side in {agree.sum()}")
    line("tape trades also traded by the book version", same.pnl_tape)
    line("tape trades the book version did not take", t_tape[~t_tape.expiry.isin(t_book.expiry)].pnl)
    return t_tape, t_book


# ============================================================================ fetching the public tape
def hourly_slug(open_s):
    """Slug of the 1h contract opening at a UTC second: bitcoin-up-or-down-november-12-2026-4pm-et."""
    t = pd.Timestamp(open_s, unit="s", tz="UTC").tz_convert("America/New_York")
    hour = t.hour % 12 or 12
    return f"{LONG_NAME}-up-or-down-{t.strftime('%B').lower()}-{t.day}-{t.year}-{hour}{'am' if t.hour < 12 else 'pm'}-et"


def contracts_for(first_day, last_day):
    """Every 15m contract of those UTC days, the 5m contract ending with it, and the 1h one at full hours."""
    start = int(pd.Timestamp(first_day, tz="UTC").timestamp())
    end = int(pd.Timestamp(last_day, tz="UTC").timestamp()) + 86_400
    rows = []
    for close in range(start + 900, end + 1, 900):
        rows.append({"slug": f"{ASSET}-updown-15m-{close - 900}", "length": "15m", "close_ts": close})
        rows.append({"slug": f"{ASSET}-updown-5m-{close - 300}", "length": "5m", "close_ts": close})
        if close % 3600 == 0:
            rows.append({"slug": hourly_slug(close - 3600), "length": "1h", "close_ts": close})
    extra = [{"slug": f"{ASSET}-updown-5m-{close - 300}", "length": "5m", "close_ts": close}      # the other 5-minute boundaries, for the gap
             for close in range(start + 300, end + 1, 300) if close % 900]
    return pd.DataFrame(rows), pd.DataFrame(extra)


def fetch_lines(wanted, path):
    """Official line, final price, outcome and condition id per contract, from the Gamma API. Resumable."""
    import json

    import requests
    have = pd.read_parquet(path) if os.path.exists(path) else pd.DataFrame(columns=["slug"])
    todo = wanted[~wanted.slug.isin(have.slug)]
    print(f"lines: {len(wanted)} wanted, {len(todo)} to fetch", flush=True)
    session, rows = requests.Session(), []
    session.headers["Accept-Encoding"] = "gzip"
    lengths = dict(zip(wanted.slug, wanted.length))
    for i in range(0, len(todo), 40):
        slugs = list(todo.slug.iloc[i:i + 40])
        events = []
        for attempt in range(5):
            try:
                r = session.get("https://gamma-api.polymarket.com/events", params=[("slug", s) for s in slugs] + [("limit", 40)], timeout=40)
                r.raise_for_status()
                events = r.json()
                break
            except (requests.RequestException, ValueError):
                time.sleep(3 * (attempt + 1))
        for e in events:
            meta, m = e.get("eventMetadata") or {}, (e.get("markets") or [{}])[0]
            prices = json.loads(m.get("outcomePrices") or "[]")
            desc = (e.get("description") or "").lower()
            rows.append({"slug": e["slug"], "length": lengths.get(e["slug"]), "close_ts": int(pd.Timestamp(e["endDate"]).timestamp()),
                         "line": meta.get("priceToBeat"), "final": meta.get("finalPrice"),
                         "up": float(prices[0]) if len(prices) == 2 and m.get("closed") else None,
                         "settles_on": "binance" if "binance" in desc else "chainlink_twap" if "twap" in desc
                                       else "chainlink_spot" if "chainlink" in desc else "unknown",
                         "condition_id": m.get("conditionId")})
        if (i // 40) % 25 == 24 or i + 40 >= len(todo):
            if rows:
                have = pd.concat([have, pd.DataFrame(rows)], ignore_index=True).drop_duplicates("slug")
                have.to_parquet(path)
                rows = []
            print(f"  lines {min(i + 40, len(todo))}/{len(todo)}", flush=True)
        time.sleep(0.1)
    return have


def fetch_trades_one(args):
    """All trades of one contract from the public tape, newest first, following the cursor."""
    import requests
    slug, condition_id = args
    out, cursor = [], None
    for _ in range(60):
        params = {"condition": condition_id, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(6):
            try:
                r = requests.get(TRADES_URL, params=params, headers={"Accept-Encoding": "gzip"}, timeout=40)
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                r.raise_for_status()
                body = r.json()
                break
            except (requests.RequestException, ValueError):
                time.sleep(2 * (attempt + 1))
        else:
            return slug, None
        out += [(slug, d["timestamp"], d["price"], d["side"], d["outcome_index"], d["size"]) for d in body["data"]]
        cursor = body["pagination"].get("next_cursor")
        if not cursor:
            break
    return slug, out


def download_candles(days):
    """Binance 1-second candles of this asset's pair for those days, from the public daily files."""
    import io
    import zipfile

    import requests
    have = pd.read_parquet(CANDLES) if os.path.exists(CANDLES) else pd.DataFrame(columns=["open_ts", "open", "close"])
    done = set(pd.to_datetime(have.open_ts, unit="ms").dt.strftime("%Y-%m-%d").unique()) if len(have) else set()
    parts = [have]
    for day in days:
        if day in done:
            continue
        r = requests.get(f"https://data.binance.vision/data/spot/daily/klines/{SYMBOL}/1s/{SYMBOL}-1s-{day}.zip", timeout=120)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            k = pd.read_csv(z.open(z.namelist()[0]), header=None, usecols=[0, 1, 4], names=["open_ts", "open", "close"])
        if k.open_ts.iloc[0] > 10 ** 14:
            k["open_ts"] //= 1000
        parts.append(k)
    pd.concat(parts, ignore_index=True).drop_duplicates("open_ts").sort_values("open_ts").to_parquet(CANDLES)
    print(f"  candles {SYMBOL}: {len(parts) - 1} days added", flush=True)


def ensure_candles(first_day, last_day):
    """Binance 1-second candles for those days: the public daily files, or, for a day Binance has not
    published yet (the current day), the candles our own recorder received."""
    import requests

    import scripts.fetch_july_reference as public
    from recorder.reader import load as load_recorded
    if ASSET != "btc":
        return download_candles(pd.date_range(first_day, last_day).strftime("%Y-%m-%d"))
    path = CANDLES
    for day in pd.date_range(first_day, last_day).strftime("%Y-%m-%d"):
        public.FIRST_DAY = public.LAST_DAY = day
        try:
            public.fetch_binance()
        except requests.HTTPError:
            start = int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)
            own = load_recorded("binance", start, start + 86_400_000 + 600_000, columns=["open_ts", "open", "close", "closed"])
            own = own[own.closed & (own.open_ts >= start) & (own.open_ts < start + 86_400_000)].drop_duplicates("open_ts")
            have = pd.read_parquet(path)
            have = have[(have.open_ts < start) | (have.open_ts >= start + 86_400_000)]      # today's part is replaced each time
            pd.concat([have, own[["open_ts", "open", "close"]]]).sort_values("open_ts").to_parquet(path)
            print(f"  binance {day}: not published yet, {len(own)} candles from our own recorder", flush=True)


def fetch(first_day, last_day, threads=4):
    os.makedirs(TAPE_DIR, exist_ok=True)
    ensure_candles((pd.Timestamp(first_day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), last_day)
    main, extra = contracts_for(first_day, last_day)
    lines = fetch_lines(pd.concat([main, extra]), os.path.join(TAPE_DIR, "lines.parquet"))
    lines = lines[lines.slug.isin(main.slug) & lines.condition_id.notna() & lines.up.isin([0.0, 1.0])]      # resolved contracts only
    for day, part in lines.groupby(pd.to_datetime(lines.close_ts - 1, unit="s").dt.strftime("%Y-%m-%d")):
        path = os.path.join(TAPE_DIR, f"trades_{day}.parquet")
        if os.path.exists(path):
            continue
        with ThreadPoolExecutor(threads) as pool:
            got = list(pool.map(fetch_trades_one, zip(part.slug, part.condition_id)))
        failed = [s for s, rows in got if rows is None]
        rows = [r for _, part_rows in got if part_rows for r in part_rows]
        frame = pd.DataFrame(rows, columns=["slug", "ts", "price", "side", "outcome_index", "size"])
        print(f"  trades {day}: {len(part)} contracts, {len(frame):,} trades, {len(failed)} failed", flush=True)
        if not failed:                      # an incomplete day is not saved, so a rerun fetches it again
            frame.to_parquet(path)


# ============================================================================ today's rule, from the tape
_NOW = {}


def now_reference():
    """Lines, outcomes, candles and the Binance-Chainlink gap for the fetched period (60-second average rule)."""
    if _NOW:
        return _NOW
    lines = pd.read_parquet(os.path.join(TAPE_DIR, "lines.parquet"))
    lines["open_ts"] = lines.close_ts - lines.length.map(LENGTH_S)
    candles = pd.read_parquet(CANDLES).set_index("open_ts").sort_index()
    chain = lines[lines.length.isin(["5m", "15m"])]
    at = pd.concat([pd.Series(chain.line.values, index=chain.open_ts.values),
                    pd.Series(chain.final.values, index=chain.close_ts.values)]).dropna()
    at = at.groupby(level=0).first().sort_index()               # the 60-second average ending at each boundary
    # Binance's own average over the same 60 seconds: candles closing at T-59 s ... T
    closes = candles.close
    full = closes.reindex(range(int(closes.index.min()), int(closes.index.max()) + 1000, 1000))
    mean60 = full.rolling(60, min_periods=50).mean()            # indexed by the open time of the last candle
    gap = pd.Series(mean60.reindex(at.index * 1000 - 1000).values - at.values, index=(at.index * 1000).astype("int64")).dropna()
    _NOW.update(lines=lines, candles=candles, average_at=at,
                gap=pd.DataFrame({"recv_ts": gap.index.values, "gap": gap.values}),
                line=lambda n, open_ms: at.get(open_ms // 1000, np.nan))
    return _NOW


def now_outcomes():
    lines = now_reference()["lines"].dropna(subset=["up"])
    lines = lines[lines.up.isin([0.0, 1.0])]
    return {(int(c) * 1000, l): float(u) for c, l, u in zip(lines.close_ts, lines.length, lines.up)}


def _now_day(path):
    ref = now_reference()
    trades = pd.read_parquet(path)
    lines = ref["lines"].set_index("slug")
    lines = lines[lines.settles_on.isin(["chainlink_twap", "binance"])]          # today's rule only
    trades = trades.join(lines[["length", "close_ts"]], on="slug", how="inner")
    tables = []
    for close, part in trades.groupby("close_ts"):
        legs = {n: t for n, t in part.groupby("length")}
        tables.append(event_table(int(close) * 1000, legs, ref, votes.AVG_WINDOW_S, lag_s=API_LAG_S))
    return pd.concat([t for t in tables if t is not None], ignore_index=True) if tables else None


PERIODS = {"now": ("2026-10-13", "2026-11-11"),          # the first test under today's rule
           "autumn": ("2026-08-20", "2026-10-12")}       # the second, back to the change of rule


def now_path(period="now"):
    return os.path.join(OUT_DIR, f"tape_{period}{SUFFIX}.parquet")


def build(period="now", workers=6):
    first, last = PERIODS[period]
    files = [f for f in sorted(glob.glob(os.path.join(TAPE_DIR, "trades_*.parquet"))) if first <= os.path.basename(f)[7:17] <= last]
    print(f"building {len(files)} days ({period}) from the public trade tape ...", flush=True)
    with ProcessPoolExecutor(workers) as pool:
        tables = list(pool.map(_now_day, files))
    signals = finish(tables, now_path(period))
    print(f"{signals.expiry.nunique()} events, {len(signals):,} seconds -> {now_path(period)}")
    return signals


def load_now(period="now"):
    return pd.read_parquet(now_path(period)) if os.path.exists(now_path(period)) else build(period)


def lag_scan():
    """Mean error of the rebuilt mid against our recorded book, for time shifts of the public tape's stamps."""
    import evidence
    own, _, _ = evidence.dataset("november")
    lines = now_reference()["lines"].set_index("slug")
    days = sorted(set(pd.to_datetime(own.expiry, unit="ms").dt.strftime("%Y-%m-%d")))
    trades = pd.concat([pd.read_parquet(f) for d in days for f in glob.glob(os.path.join(TAPE_DIR, f"*_{d}.parquet"))])
    trades = trades.join(lines[["length", "close_ts"]], on="slug")
    rows = {}
    for lag in range(0, 9):
        errors = {n: [] for n in ("5m", "15m", "1h")}
        for close, part in trades.groupby("close_ts"):
            o = own[own.expiry == close * 1000]
            for n, t in part.groupby("length"):
                if len(o) and f"bid_{n}" in o:
                    bid, ask, _ = quotes_from_trades(t, o.ts.values, lag)
                    e = ((bid + ask) - (o[f"bid_{n}"].values + o[f"ask_{n}"].values)) / 2 * 100
                    errors[n].append(np.abs(e[np.isfinite(e)]))
        rows[lag] = {n: np.mean(np.concatenate(v)) if v else np.nan for n, v in errors.items()}
    print("Mean error of the rebuilt mid in cents, by the lag assumed for the tape's time stamps (seconds):")
    print(pd.DataFrame(rows).T.round(2).to_string())


def check():
    """Under today's rule: the tape version against our own recorder on the expiries both have.

    Needs the public trades of the recorder's days: trades_<day>.parquet inside the test period,
    check_<day>.parquet for days after it (fetch the day, then rename the file so that the test
    period stays as it was fixed)."""
    import backtest
    import evidence
    own, outcomes, _ = evidence.dataset("november")
    _NOW.clear()
    days = sorted(set(pd.to_datetime(own.expiry, unit="ms").dt.strftime("%Y-%m-%d")))
    files = [f for d in days for f in glob.glob(os.path.join(TAPE_DIR, f"*_{d}.parquet"))]
    tape = pd.concat([_now_day(f) for f in files], ignore_index=True)
    tape = tape[tape.expiry.isin(own.expiry.unique())].sort_values(["expiry", "ts"]).reset_index(drop=True)
    own = own[own.expiry.isin(tape.expiry.unique())].sort_values(["expiry", "ts"]).reset_index(drop=True)
    print(f"Expiries in both: {own.expiry.nunique()} ({backtest.fmt_ts(own.expiry.min())} to {backtest.fmt_ts(own.expiry.max())} UTC)\n")
    both = own.merge(tape, on=["expiry", "ts"], suffixes=("", "_tape"))
    rows = []
    for n in ("5m", "15m", "1h"):
        d = both[[f"bid_{n}", f"ask_{n}", f"bid_{n}_tape", f"ask_{n}_tape", f"spot_{n}", f"spot_{n}_tape", f"line_{n}", f"line_{n}_tape",
                  f"v_{n}", f"v_{n}_tape"]]
        q = d[[f"bid_{n}", f"ask_{n}", f"bid_{n}_tape", f"ask_{n}_tape"]].dropna()
        mid_err = ((q[f"bid_{n}_tape"] + q[f"ask_{n}_tape"]) - (q[f"bid_{n}"] + q[f"ask_{n}"])) / 2 * 100
        spot_err = (d[f"spot_{n}_tape"] - d[f"spot_{n}"]).dropna()
        line_err = (d[f"line_{n}_tape"] - d[f"line_{n}"]).dropna()
        v = d[[f"v_{n}", f"v_{n}_tape"]].dropna()
        rows.append({"contract": n, "seconds": len(q), "mid error, typical c": mid_err.abs().median(), "95th pct c": mid_err.abs().quantile(0.95),
                     "spot error sd $": spot_err.std(), "spot error robust $": 1.4826 * (spot_err - spot_err.median()).abs().median(),
                     "line error max $": line_err.abs().max(),
                     "vote ratio median": float(np.exp(np.log(v[f"v_{n}_tape"] / v[f"v_{n}"]).median())) if len(v) else np.nan})
    print("1. Inputs, tape version against the recorder:")
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    for label, t in (("recorder", own), ("tape", tape)):
        voted = (t.n_votes >= 2).sum()
        print(f"  {label}: seconds with 2+ votes {voted:,}, signal on {(t.kind != 'none').sum() / max(voted, 1):.1%}")
    agree = both[(both.n_votes >= 2) & (both.n_votes_tape >= 2)]
    print(f"  same second, both voting: signal in both {((agree.kind != 'none') & (agree.kind_tape != 'none')).sum():,}, "
          f"recorder only {((agree.kind != 'none') & (agree.kind_tape == 'none')).sum():,}, "
          f"tape only {((agree.kind == 'none') & (agree.kind_tape != 'none')).sum():,}")
    print("\n2. Trades under the frozen rules (cents per trade, before fees):")
    t_own, _ = backtest.simulate(own, outcomes, backtest.DEFAULT_THRESHOLD)
    t_tape, _ = backtest.simulate(tape, outcomes, backtest.DEFAULT_THRESHOLD)
    for label, t in (("recorder", t_own), ("tape", t_tape)):
        pnl = t.pnl.dropna() * 100
        print(f"  {label:<9} {len(pnl)} trades, won {(pnl > 0).mean():.0%}, {pnl.mean():+.1f} c")
    same = t_tape.merge(t_own, on="expiry", suffixes=("_tape", "_own"))
    ok = (same.side_tape == same.side_own) & (same.lo_tape == same.lo_own) & (same.hi_tape == same.hi_own)
    print(f"  expiries traded by both: {len(same)}, same legs and side in {ok.sum()}")
    show = pd.concat([t_own.assign(source="recorder"), t_tape.assign(source="tape")]).sort_values(["expiry", "source"])
    print(show.assign(expiry=show.expiry.map(backtest.fmt_ts))[["expiry", "source", "tau", "kind", "lo", "hi", "side", "edge_at_entry", "price", "pnl"]]
          .round(3).to_string(index=False))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "validate":
        validate()
    elif command == "fetch" and len(sys.argv) == 4:
        fetch(sys.argv[2], sys.argv[3])
    elif command == "build":
        build(sys.argv[2] if len(sys.argv) > 2 else "now")
    elif command == "check":
        lag_scan()
        check()
    else:
        sys.exit(__doc__)
