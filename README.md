# Polymarket BTC same-expiry study

Polymarket lists BTC "Up or Down" contracts of 5 minutes, 15 minutes and 1 hour. Contracts of
different lengths often expire at the same instant but have different lines (strikes), so at one
expiry there are two or three prices for the same final BTC move. This project records those
markets and tests whether the prices disagree with each other by enough to trade.

Everything here is read-only market data. Nothing places orders, and nothing touches `trader.py`.

## Status (2026-11-12)

- **Recorder:** recording since 2026-11-11 20:53 UTC (one 30-second restart on 2026-11-12) as the
  Windows task `PolyRecorder`, writing to
  `C:\polydata`. Our recorded line matched the official one to the cent on every window checked.
- **Analysis engine:** built and tested; the brief's acceptance numbers reproduce. On simulated
  markets it trades nothing when prices are fair, and when one contract is quoted 6 or 10 cents off
  it names that contract and earns about the edge it predicted (`simulation_check.py`).
- **Measurement on real data: works.** Contracts that expire together disagree by 0.96 error bars
  in the typical second, where 1.0 means the error bars are exactly the right size.
- **Strategy result on real data: not shown yet.** After a lasting signal the price moves +2.4 cents
  toward the model in 30 seconds (standard error 1.2, predicted 3.8), but on only 25 signals and on
  data the last fix was designed on. Held to expiry there are 3 trades, all losers; about 280 are
  needed for +/- 4.5 cents.
- **The rules are frozen as of 2026-11-12 11:00 UTC.** Data after that is the honest test:
  `python backtest.py 2026-11-12T11:00 2026-11-30`.

Details, including everything that differs from the original brief, are in `FINDINGS.md`.

## The idea in five lines

1. Model the BTC move left until expiry as a fat-tailed (Student-t) distribution with one unknown: its width.
2. Each contract's price and line imply a width. That is the contract's "vote".
3. Contracts expiring together should vote the same. Realised volatility over the last hour gives one more vote.
4. If one contract disagrees with the rest by more than 2.5 error bars, it is the outlier.
5. Trade the range between the outlier's line and the neighbouring line at the width the others agree on, if the edge after the bid-ask spread is at least 4.5 cents.

## Commands

Run from this folder with the project's Python (`C:\polydata\venv\Scripts\python`).

| Command | What it does | Time |
|---|---|---|
| `python backtest.py` | Full strategy report on everything recorded; ends with a short summary | about 1 min per new day |
| `python votes.py 2026-11-12T05:00` | Second-by-second prices, votes and signals for one expiry (add `all` for every second) | seconds |
| `python daily_check.py` | Data quality for yesterday: gaps, coverage, line error, outcomes reproduced | about 1 min |
| `python fit_nu.py` | Refits the tail parameter from 90 days of Binance candles into `nu.json` | 1 min first time |
| `python variants.py` | The frozen rules and alternatives side by side, on all data and on data after the freeze; every run logged to `results/` | about 5 min |
| `python simulation_check.py` | Simulated markets with a known mispricing: is it found, and is nothing traded when prices are fair? | 2 min |
| `python -m unittest discover tests` | 36 tests | 2 s |
| `python -m recorder.acceptance <start> <end>` | Recorder report for a UTC window | seconds per hour |

Reports are saved in `C:\polydata\reports`. Plots are in `C:\polydata\analysis` as SVG files that
open in any browser: `qq_5m.svg`, `qq_15m.svg`, `qq_60m.svg` (tail fit) and `calibration.svg`.

## Reading the backtest summary

    Events: 41 (...), 4 of them with all three legs voting at some point.
    Signals: on 12.0% of the 7,670 seconds with 2+ votes; 34% of tradable signals last 3 s or more.
    Trades (second part, threshold 4.5 c): 1. Fewer than 30 trades: treat P&L as not yet measured.
    After a lasting signal (32 signals in 8 events): the price moved +1.6 c toward the model in 30 s (se 1.2), against an edge of 3.0 c seen: no measurable move.
    False alarms: a signal on 9.5% of quiet seconds, a tradable one on 1.3%.
    Calibration (143 markets): Brier 0.1381 normal, 0.1400 t (0.25 = coin flip).
    Strikes out of order at executable prices: 5m vs 15m 25 s of 7,044, ...

- **Events** are expiries shared by two or more contracts. The first 60% (by time) are used to pick
  the edge threshold, the rest are reported.
- **Signals** are seconds where the contracts disagree. A signal is traded only after it has held
  for 3 seconds, at the next second's quotes, one position per expiry, held to expiry.
- **Trades** are in cents per share with a 95% interval. One trade wins or loses tens of cents, so
  the interval is wide until there are many.
- **After a lasting signal** is the quickest evidence either way: if signals are real mispricings,
  the price should move toward the model's value by roughly the edge within a minute.
- **False alarms** are signals while the market is calm, when contracts should agree.
- **Calibration** asks whether the model's probabilities come true, for a normal and a t distribution.
- **Strikes out of order** counts seconds where a lower line's Yes could be bought below a higher line's bid.

## Files

| File | Purpose |
|---|---|
| `recorder/` | The recorder and its own README (how to run, check and move it) |
| `daily_check.py` | Morning data-quality check |
| `fit_nu.py`, `nu.json` | Tail fit and its result |
| `votes.py` | The model: votes, error bars, outlier, range trade; builds the per-second table for an event |
| `backtest.py` | Simulation and report |
| `variants.py`, `results/` | Strategy variants and the logged history of every run (`variant_runs.csv`, `VARIANTS.md`, `HISTORY.md`) |
| `simulation_check.py` | The same engine on simulated markets where the truth is known |
| `svgplot.py` | Small plot writer (SVG), so no plotting library is needed |
| `tests/` | Unit tests, including the brief's worked example |
| `FINDINGS.md` | What was verified, what differs from the brief, open questions |

## Known limits

- No fees and no size limits are modelled.
- Our Chainlink price arrives about 1.5 seconds after its timestamp, and the market reacts to
  Binance sooner than that. The error bars and the 3-second rule exist to keep that lag from
  looking like mispricing.
- The hourly contract settles on Binance and the others on Chainlink, which differ by about $90, so
  a trade that mixes them is not a pure range.
- The recorder runs on a desktop PC: after a reboot it resumes only once the user logs in.
- The public Chainlink feed is deprecated by Polymarket and could be switched off.

## Shared dataset

A teammate shares a dataset over Tailscale (SFTP; host and user are in the env file below, not in this public repo). It is mirrored into
`C:\polydata\shared` (`<POLYDATA_DIR>/shared`): `README.txt` and `DATA_INVENTORY.md` at the top,
the data under `data/`. About 800 MB at first; `data/events/` (a few GB of Parquet) arrives later.

- **Pull or refresh:** `C:\polydata\venv\Scripts\python scripts\pull_shared.py`. Files already here
  with the remote's size are skipped, so re-run it whenever the remote has grown. It ends by
  listing every file against the remote and describing each Parquet (columns, rows, first and last
  timestamp). `--verify` compares without downloading, `--offline` describes the local copy only.
- **Needs:** Tailscale connected with the teammate's machine visible (`tailscale ping <host>`),
  and `SHARED_SFTP_HOST=...`, `SHARED_SFTP_USER=...` and `SHARED_SFTP_PASSWORD=...` in
  `C:\polydata\shared_sftp.env` (not in the repo).
- **Load:** `from recorder import load_shared`, then `load_shared.datasets()` for the names and
  `load_shared.load("historical/contracts")`, with optional `columns=`, `start=`, `end=`.
  Timestamps come back in UTC. For the tick extracts: `load_shared.events_index()` and
  `load_shared.events("2026-07-11 18:30")`.
- **Rule:** analysis only reads from `shared/`. Nothing derived is written there. Derived files go
  to `C:\polydata\analysis` or `C:\polydata\reports` with the date in the name, for example
  `analysis\events_summary_2026-11-12.parquet`.

**Pulled 2026-11-12 (694 files, 1,344 MB, every size equal to the remote):**

| Dataset name | Rows | Covers (UTC) | Content |
|---|---|---|---|
| `historical/contracts` | 11,289 | 2026-04-23 to 07-01 | Contract list, all five durations: line (`priceToBeat`), `finalPrice` |
| `historical/trades` | 10,233,096 | 2026-04-22 to 05-07 | Trade tape, all durations (723 MB; pass `columns=` or `start=`/`end=`) |
| `historical/trades_1h_june` | 744,469 | 2026-05-31 to 07-01 | Trade tape, 1h contracts |
| `historical/prices_1m` | 172,043 | 2026-05-31 to 06-30 | One mid per token per minute, no bid/ask |
| `historical/binance_1m` | 102,624 | 2026-04-23 to 07-03 | Binance BTCUSDT 1-minute candles |
| `historical/binance_1s` | 2,620,978 | 2026-05-31 to 07-01 | Binance BTCUSDT 1-second closes |
| `historical/chainlink_btc_usd` | 35,900 | 2026-04-23 to 05-07 | Chainlink on-chain rounds, about 34 s apart |
| `orderbook_rest/orderbook_rest_dedup` | 2,553,573 | 2026-05-06 to 07-22 | Best bid/ask every 10 s, all durations; gap-free from 07-02 |
| `events/<YYYYMMDD_HHMMSS>` | 45,216,455 in 681 files | 2026-07-11 18:15 to 07-18 20:45 | Tick extract per shared expiry (file name = expiry): book, trade and Binance rows for the 15 minutes before |
| `events/index` | 7 so far | | One row per extracted expiry: legs, lines, outcomes, row counts |

`data/events/` was still being written on the remote at the time of the pull (it should reach
2026-07-22), so re-run the pull. `load("events")` is all event files as one table; with
`start=`/`end=` it reads only the files whose window overlaps (six hours is about 1.3M rows, under
a second). Any load of more than 500 MB on disk without `columns=` is refused (`max_bytes=None`
overrides), which today means `historical/trades`; with five columns it is 266 MB in memory.
`data/historical` is not one dataset, because its files have different columns.

Things to know about the event extracts, checked on the pulled files:

- `ts_ms` is the recording PC's receive time and `ex_ms` the exchange's time (Binance and trade
  rows only; book rows have no exchange time). `ts_ms - ex_ms` is 12.8 s on median for both Binance
  and Polymarket trades (min 9.1 s, 99th percentile 16.5 s, over 20M rows), far more than network
  delay, so the recording PC's clock was about 13 s off. Use `ex_ms` where it exists. Book rows are
  stamped on the offset clock: subtract about 12.8 s before lining them up with anything else.
- Each file covers `ts_ms` from T-15 min to T+5 s. If the offset is right, the last ~8 s before
  expiry in real time are not in the file.
- `events/index` lists only the 7 events of a first test extraction (and `extract_log.txt` is from
  that run); use the file names for the full list. Outcomes and settlement prices are null by
  design (the tape has none); take them from `historical/contracts` where it overlaps (it ends
  2026-07-01, so not for these July events) or from Binance. `shared\DATA_INVENTORY.md` has the full schemas and gap maps; the
453 GB tick tape itself is not shared (`shared\data\TICK_TAPE_NOTE.txt`).
