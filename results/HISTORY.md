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
