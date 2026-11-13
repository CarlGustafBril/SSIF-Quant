# Polymarket BTC same-expiry study

Polymarket lists BTC "Up or Down" contracts of 5 minutes, 15 minutes and 1 hour. Contracts of
different lengths often expire at the same instant but have different lines (strikes), so at one
expiry there are two or three prices for the same final BTC move. This project records those
markets and tests whether the prices disagree with each other by enough to trade.

Everything here is read-only market data. Nothing places orders, and nothing touches `trader.py`.

## Status (2026-11-13, 09:45 UTC)

**In one line:** contracts that expire together do disagree by more than noise, and the price does
move toward our model afterwards, but under today's settlement rule the trade built on it does not
cover the exchange's fee, and resting orders instead of taking do no better. The signal is real as a measurement and not a strategy to fund as it stands.

| Data | Settlement rule | Shared expiries | Trades | Per trade before fees | After fees |
|---|---|---|---|---|---|
| Teammate's July tick recording (two deliveries) | old (spot price) | 1,056 | 164 | +8.5 c (se 3.1) | +6.3 c (+0.2 to +12.3) |
| Teammate's July 3-second polls, other expiries | old | 940 | 63 | +1.0 c (se 6.0) | -1.1 c |
| Public trade tape, 08-20 to 10-12 | **today's (60-second average)** | 5,140 | 544 | +2.2 c (se 1.7) | -0.9 c (-4.3 to +2.5) |
| Public trade tape, 10-13 to 11-11 | **today's** | 2,869 | 252 | +0.2 c (se 2.5) | -2.9 c (-7.9 to +2.0) |
| Our own recording since 11-11 | **today's** | 111 | 6 | -40.7 c (se 15.9) | -42.8 c |
| **All of today's rule** | | 8,120 | 802 | **+1.2 c (se 1.4)** | **-1.9 c (95% interval -4.6 to +0.9)** |

All rows use the rules frozen on 2026-11-12 11:00 UTC, unchanged: one range trade per expiry, 4.5
cents minimum edge, held to expiry. "se" is the standard error. The fee is the exchange's taker fee,
0.07 x p x (1 - p) per share and fill, about 2.2 cents for the two legs.

**How this was established.** Each test after the first was fixed in writing and committed before
its data was fetched (`results/HISTORY.md`): the second July delivery, then 30 days of today's rule,
then the eight weeks back to the change of rule.

- **Old rule (July): the trade worked.** +8.5 c per trade before fees on tick data, 2.7 standard
  errors from zero, and the second delivery repeated the first (+8.6 c against +8.4 c).
- **The rule changed between 5 and 20 August 2026.** The 5m and 15m contracts now settle on a
  60-second average. Our own recording is one day old, so today's rule was tested on Polymarket's
  public trade tape, with best bid and ask rebuilt from the last trades on each side (`tape.py`).
  On July, where both exist, the rebuilt quotes give the same trades as the recorded book.
- **Today's rule: no profit after fees.** 802 trades over 12 weeks: +1.2 c before fees, -1.9 c
  after. The first test said "not worth trading as it stands" by the rule written beforehand (upper
  end of the interval below +3 c), and the second, larger one agreed.
- **The tuning that was tried failed its own test.** Trading only the 5m and 15m pair (`same_feed`)
  looked good on three samples (+7.9, +10.4, +7.4 c before fees). On the eight weeks it had not
  seen it made +1.0 c before fees and -2.1 c after (216 trades), which rejects it. A higher edge
  threshold (`edge_8`) did no better than the baseline under today's rule.
- **What does hold under both rules:** after a signal that lasts 3 seconds the price moves 1.4 to
  2.1 cents toward the model within 30 seconds (standard errors 0.2 to 0.5, about 4,700 signals).
  That is a fifth of the edge the model sees, and less than the spread and fee it costs to trade.
- **Size is small in any case:** a median of 53 shares at the best price at the fill in July.

- **Resting orders do not rescue it.** The same signals executed with orders resting at the best
  price for 30 seconds (no taker fee; counted as filled only when the market trades through them)
  made -2.0 c per signal under today's rule (796 signals, 95% interval -4.7 to +0.8), specified
  before it was run (`maker.py`). The orders fill mostly when the signal was wrong.

- **Error bars and the distribution were checked and do not change the answer (2026-11-13).** The
  error bar of a vote is almost entirely the allowance for the age of the reference price. With
  that allowance measured instead of assumed (`bars.py`), the bars are better sized and the trade
  makes +2.4 c before fees under today's rule (1,166 trades, se 1.2) against +1.6 c; with an
  empirical return distribution instead of the Student-t (`empirical.py`) it makes +2.7 c (769
  trades, se 1.4). Both were specified before being run. After fees all three are below zero.
  Why a Student-t was chosen, with the evidence, is in `FINDINGS.md`, Phase 5.

- **Other coins (ETH, SOL, XRP): untested, not rejected.** The same rules look profitable there on
  quotes rebuilt from public trades (+5.2 c after fees, 519 trades), but that is an artefact: those
  markets trade too rarely to rebuild a quote, and at prices that really traded the result is
  -1.2 c (89 trades, se 3.9). Only a recording of their order books can test them (`assets.py`).

**Bottom line for the committee:** every route tested under today's rule ends at or below zero:
taking (-1.9 c after fees), resting orders (-2.0 c), and two rule changes that failed on unseen
data. More recording of the same thing will not change this: 802 trades already put the taker
result between -4.6 and +0.9 cents. What can be presented is a sound method, a result that held
under the old rule, and a clear reason not to trade it now. The one open lead is the thinner
markets (ETH, SOL, XRP), which need their own order-book recording before anything can be said.

- **Recorder:** running since 2026-11-11 20:53 UTC as the Windows task `PolyRecorder`, writing to
  `C:\polydata`. Our recorded line matched the official one to the cent on every window checked.
- **Rules:** unchanged since the freeze. `same_feed` and `edge_8` stay in `variants.py` as tested
  and rejected alternatives.

Details are in `FINDINGS.md` (Phase 3: July, Phase 4: today's rule); every rule version, test and
result in `results/HISTORY.md`.

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
| `python evidence.py summary` | The table above: the frozen rules on every dataset, pooled, with fees, size and the sample still needed | 1 min |
| `python evidence.py july` (or `holdout`, `polls`, `november`, `now`, `autumn`) | Threshold-free tests on one dataset: does the other contract's width predict the outcome or the next price move? Add `--variants` for every rule variant | 1 to 10 min |
| `python july.py build` | Rebuilds the July signal tables from the shared dataset (needs the two steps under "Shared dataset" first) | 3 min |
| `python scripts/fetch_july_reference.py` | Official July lines and outcomes (Polymarket) and Binance 1-second candles, into `C:\polydata\reference` | 5 min |
| `python assets.py pooled` | The same rules on ETH, SOL and XRP (after `tape.py fetch` and `build` with `POLY_ASSET` set), at rebuilt quotes and at real traded prices | 1 min |
| `python bars.py measure` | The error of each kind of reference price, measured on our recording and July (the numbers behind the `measured_bars` variant) | 1 min |
| `python empirical.py compare` | The empirical return distribution against the fitted Student-t (`empirical.py build` rewrites `empirical.json`) | 10 s |
| `python evidence.py variants <data> <variant> ...`, then `python evidence.py tape-pooled <variant> ...` | Chosen variants on one dataset; then both public-tape periods together | 10 to 60 min |
| `python maker.py` | The frozen signals executed with resting orders instead of taking (specification in `results/HISTORY.md`) | 2 min |
| `python tape.py validate` / `check` | The trade-built quotes against the recorded book: on July, and on our own recording under today's rule | 3 min |
| `python tape.py fetch <first day> <last day>`, then `python tape.py build now` | Public lines, candles and trades for a period, and its signal table (periods are named in `tape.PERIODS`) | 40 s per day |
| `python -m unittest discover tests` | 67 tests | 2 s |
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
| `july.py` | The same rules on the teammate's July recording (tick extracts and 3-second polls) |
| `evidence.py` | The signal tests and the pooled summary across datasets |
| `assets.py` | The same rules on other coins, with the check of fills against real trades |
| `bars.py` | Measured error bars for the reference price, and the Chainlink price moved forward with Binance |
| `empirical.py` | Empirical return distribution as an alternative to the Student-t |
| `maker.py` | Resting-order execution of the same signals, fills simulated from public trades |
| `tape.py` | The same rules on Polymarket's public trade tape, with quotes rebuilt from trades (today's settlement rule) |
| `scripts/` | `pull_shared.py` (mirror the shared dataset), `fetch_july_reference.py` (public reference data for July) |
| `svgplot.py` | Small plot writer (SVG), so no plotting library is needed |
| `tests/` | Unit tests, including the brief's worked example |
| `FINDINGS.md` | What was verified, what differs from the brief, open questions |

## Known limits

- The taker fee is included (`pnl_net`); size limits and our own price impact are not. Every
  trade is one share per leg at the best price.
- Under today's rule the long test uses quotes rebuilt from public trades, checked against recorded
  books but not a recording itself (`tape.py`, `FINDINGS.md` Phase 4).
- The July test stands in for the missing Chainlink feed with Binance minus the gap known at each
  5-minute boundary, and July's settlement rule differs from today's (`july.py`, `FINDINGS.md`).
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

**Pulled 2026-11-12 in two deliveries (first 694 files, 1,344 MB; then 375 more tick extracts at 22:10 UTC):**

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
| `events/<YYYYMMDD_HHMMSS>` | 1,056 files (45,216,455 rows in the first 681) | 2026-07-11 18:15 to 07-22 19:15 | Tick extract per shared expiry (file name = expiry): book, trade and Binance rows for the 15 minutes before |
| `events/index` | 7 so far | | One row per extracted expiry: legs, lines, outcomes, row counts |

The first delivery ended at the 07-18 20:45 expiry; the rest arrived later the same day and is
kept apart as a test set (`july.SEEN_UNTIL`). `load("events")` is all event files as one table; with
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
