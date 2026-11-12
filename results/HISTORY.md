# Rule versions and their results

Every version of the strategy rules, in the order they were made, with the results the backtest
gave at the time. From 2026-11-12 17:00 UTC on, every evaluation is also logged automatically in
`variant_runs.csv` by `python variants.py`, with the git version that produced it.

How to read the numbers:

- **Trades** in the backtest rows below are trades in the *reported* part of the data (the last 40%
  of events), held to expiry, in cents per share. `variants.py` instead counts every trade.
- **Price move** is how far the range's mid moved toward the model 30 s after a signal that held
  3 s, against the edge the model saw. A real mispricing shows a move of roughly that edge.
- **Error-bar fit** is the typical disagreement between contracts in error bars; 1.0 means the
  error bars are the right size.

All times UTC, 2026-11-12.

## v0: the brief's rules

Error bars from tick size and line error only. Signal at 2.5 error bars, trade at the first second
with 4.5 cents of edge, held to expiry.

| When | Events | Signal rate | Trades (reported part) | Notes |
|---|---|---|---|---|
| 05:47 | 38 | 61% of seconds with 2+ votes | 13, hit rate 15%, -30.6 c (-48.1 to -13.1) | Our line error is zero, so the error bars were almost only the tick |

## v1: staleness term added

Error bar widened by spot x realised vol (last 60 minutes) x sqrt(age of our spot price).
Measured: our Chainlink price is about 2.1 s old and about $5 off.

| When | Events | Signal rate | Trades (reported part) | Notes |
|---|---|---|---|---|
| 05:49 | 38 | 12-13% | 4, -23.0 c (-82.6 to +36.6) | |

## v2: fill re-checked, threshold fixed until 30 trades

The fill is at the next second's quotes and only if the edge is still at least the threshold.
The threshold stays at 4.5 cents until the first 60% of the data holds 30 trades.

| When | Events | Signal rate | Trades (reported part) | Notes |
|---|---|---|---|---|
| 06:19 | 40 | 12% | 2, -51.5 c | One more event had a signal whose edge was gone at the fill |

## v3: signal must hold 3 seconds

41% of tradable signals lasted 1 s and 25% 2 s: the market reacting before our feed.

| When | Events | Signal rate | Trades (reported part) | Price move after 30 s | Error-bar fit |
|---|---|---|---|---|---|
| 06:22 | 40 | 12% | 1, -71 c | | |
| 06:38 | 40 | 12% | 1 | +1.6 c (se 1.2) vs 3.0 c edge, 32 signals in 8 events | 0.98 |
| 07:57 | 46 | 15.7% | 3, -33.7 c (-116.4 to +49.1) | +0.8 c (se 0.9) vs 2.7 c, 61 signals in 12 events | 1.08 |
| 08:09 | ~48 | 18.7% | 4, -30.8 c (-75.0 to +13.5) | +0.9 c (se 0.6) vs 2.5 c, 82 signals in 13 events | 1.17 |
| 10:38 | 57 | 18.5% | 7, hit rate 14%, -25.7 c (-53.5 to +2.1) | +1.0 c (se 0.6) vs 2.6 c, 87 signals in 14 events | 1.17 |

The error bars drifted too tight as the market woke up in the morning.

## v4: faster volatility, no vote without volatility (the frozen rules)

The staleness term uses the larger of the 60-minute and the last-5-minute realised vol. A contract
does not vote when no volatility figure exists (after the two PC reboots it had silently got no
staleness allowance at all).

| When | Events | Signal rate | Trades (reported part) | Price move after 30 s | Error-bar fit |
|---|---|---|---|---|---|
| 10:40 | 57 | 6.9% | 3, -47.6 c (-125.5 to +30.3) | +2.4 c (se 1.2) vs 3.8 c, 25 signals in 6 events | 0.96 |

**Rules frozen at 11:00 UTC.** Data after that is the clean test.

## Reported alongside, rules unchanged

| When | What | Result |
|---|---|---|
| 11:11 | Disagreement-limit scan (3.0 to 5.0 error bars), picked by signal rate only | 4.0 brings signals to 0.7-1.0% of seconds; 2 lasting signals in 59 events, no trades; stricter limits did not give a better price move |
| 16:26 | Post-freeze test, 11:00 to 16:15 | 22 events, signals on 2.5% of seconds, 4 lasting signals in 2 events, price moved -6.4 c (se 2.5) against 11.5 c predicted, no trades; error-bar fit 0.94 |
| 16:26 | Everything, 80 events | +1.2 c (se 1.2) vs 4.9 c, 29 signals in 8 events; the five trades so far all lost, all at full-hour expiries |
| 17:07 | `variants.py` started | See `variant_runs.csv` and `VARIANTS.md` |

## July data from the teammate's recording (2026-11-12, evening)

The frozen rules (v4) were run unchanged on the teammate's July recording: `july.py`, `evidence.py`.
July had a different settlement rule (5m and 15m settled on the Chainlink spot price at the window's
ends, no 60-second average), and the dataset has no Chainlink feed, so Chainlink is taken as Binance
minus the gap known at each 5-minute boundary. Fees are now included (`FEE_RATE` in `backtest.py`).

| When (UTC) | Data | Events | Trades | P&L before fees | After fees | Price move after 30 s |
|---|---|---|---|---|---|---|
| 21:54 | July tick extracts, 07-11 18:30 to 07-18 20:45 | 681 | 106, hit rate 67% | +8.4 c (se 3.7) | +6.2 c | +1.7 c (se 0.4) vs 7.9 c edge |
| 22:04 | July 3-second polls, other dates 07-02 to 07-22 | 1,315 | 90, hit rate 63% | +3.1 c (se 5.0) | +0.9 c | |
| 22:04 | Both July samples | 1,996 | 196 | +6.0 c (se 3.0) | +3.8 c (se 3.0) | |
| 22:04 | Our recording (current rule) | 103 | 6, one winner | -40.7 c (se 15.9) | -42.8 c | |

### Written down before looking: the test on the remaining tick extracts

At 22:10 UTC the share turned out to hold tick extracts from 07-18 21:00 onward that had not been
pulled or looked at. They are the cleanest test available, so the expectations are fixed here first.
Rules, thresholds, persistence (3 s), fill delay (1 s) and fee are exactly as in the code at this
commit. Three rule sets are judged, and nothing else:

1. **baseline** (frozen v4).
2. **same_feed**: 5m and 15m only. Proposed today because 4 of the 6 losing November trades had the
   hourly contract as one leg, and under the current rule the 15m implies a width 13% wider than the
   1h (no such gap in July).
3. **edge_8**: baseline with 8 cents minimum edge. Proposed today because the fee takes up to 3.5
   cents and July P&L rose with the edge at entry.

What would count as support: P&L per trade after fees above zero for the rule set, and a price move
toward the model 30 s after a lasting signal. The sample will be about 380 expiries, so about 50
trades and a standard error near 6 cents: it cannot settle the question alone. It will be reported
whatever it shows, alone and pooled with the earlier July trades.

### Outcome (22:17 UTC, 375 expiries, 07-18 21:00 to 07-22 19:15)

| Rule set | Trades | Won | P&L before fees (95% interval) | After fees | Price move after 30 s |
|---|---|---|---|---|---|
| baseline | 58 | 60% | +8.6 c (-2.7 to +20.0) | +6.4 c | +2.1 c (se 0.5) vs 7.2 c edge |
| same_feed | 32 | 69% | +10.4 c (-2.9 to +23.6) | +8.4 c | +2.0 c (se 1.2) vs 9.2 c edge |
| edge_8 | 38 | 63% | +13.4 c (-2.1 to +29.0) | +11.0 c | as baseline |

All three meet what was written down: positive after fees, price moving toward the model. None is
significant alone, as expected. Both tick deliveries together, baseline: 164 trades, +8.5 c before
fees (se 3.1), +6.3 c after (95% interval +0.2 to +12.3). Caveat: the 3-second polls for these four
days had been run before the expectations were written (27 trades, +7.9 c, se 9), so the period was
not wholly unseen; its tick data was. With the tick data in hand, the poll sample now counts only
expiries without tick data (940 expiries, 63 trades, +1.0 c before fees, se 6.0).

The rules of record are unchanged (v4). `same_feed` and `edge_8` are tracked alongside in
`variants.py`. Results by variant: `variants_july.csv` (first delivery, each half),
`variants_holdout.csv`, `variant_runs.csv` (our recording).

## Today's settlement rule, from the public trade tape (2026-11-12, late evening)

The 5m and 15m contracts changed from the spot price to the 60-second average between 2026-08-05
and 2026-08-20 (rules text on Gamma). Our own recording under that rule is one day old. Polymarket
publishes every trade (data-api v2), so best bid and ask can be rebuilt from the last trades on
each side: `tape.py`.

**The stand-in was checked on July first**, where book and trades both exist (1,056 expiries):

| Check | Result |
|---|---|
| Rebuilt mid against the book's mid | median error 0, typical size 0 to 0.25 c, 95th percentile 2 to 3 c |
| Frozen rules on book quotes | 164 trades, +8.5 c before fees (se 3.1) |
| Frozen rules on trade-built quotes | 132 trades, +11.0 c (se 3.5); 124 on expiries the book version also traded |
| The same 132 trades filled at the real book | +10.1 c, so the tape flatters fills by 0.9 c per trade |

### Written down before fetching: the test on 2026-10-13 to 2026-11-11

Nothing from this period has been fetched or looked at except our own recording from 11-11 20:53.

- **Data:** every shared expiry of those UTC days (about 2,880), quotes rebuilt from the public
  tape, official lines and outcomes, Binance 1-second candles, Chainlink taken as Binance minus the
  gap at each 5-minute boundary (measured against Binance's own 60-second average, since the line
  is now an average). Rules, 3 s persistence, 1 s fill delay, fee: exactly as in the code at this commit.
- **Judged:** baseline (the rule of record), `same_feed`, `edge_8`. Nothing else.
- **Figure that counts:** P&L per trade after fees, less the 0.9 c by which the tape flatters fills.
- **Supported:** that figure is more than two standard errors above zero for the baseline.
- **Not worth trading as it stands:** the upper end of its 95% interval is below +3 c.
- **Otherwise:** undecided, and the size of the interval says how much more is needed.
- **Check of the method under today's rule:** on the expiries our recorder also has, the tape
  version's trades are compared with the recorder's. Reported whatever it shows.
