# Handoff (2026-11-15)

Context for a teammate picking this up: what the project tests, what was done and why, where it
stands, and what is still open. The numbers are in `FINDINGS.md`; every rule version, test and
result, in the order they happened, is in `results/HISTORY.md`. `README.md` has the commands.

## The idea in one paragraph

Polymarket lists BTC "Up or Down" contracts of 5 minutes, 15 minutes and 1 hour. When two of them
expire at the same moment, their prices should tell the same story about how much BTC can still
move. Each price is turned into a "vote": the width of the return distribution that would make
that price fair. If two votes differ by more than 2.5 combined error bars, one contract is
mispriced relative to the other, and we buy the range between the two strikes. Everything here is
read-only market data. Nothing places orders.

## The frozen rules

Fixed on 2026-11-12 and not changed since. New ideas go in as variants next to them
(`variants.py`), never as edits.

- Return distribution: Student-t, tail parameter fitted on 90 days of Binance returns (`nu.json`).
- Signal: votes differ by more than 2.5 error bars, the edge is at least 4.5 c, and it lasts 3 s.
- Trade: the range between the two strikes, filled 1 s later, one per expiry, held to expiry.

## Where it stands

The signal is positive before fees, not statistically significant, and below zero after fees.

Both public-tape periods under today's settlement rule, 2026-08-20 to 11-11, cents per share:

| Version | Trades | Before fees (se) | t | After fees and fill optimism (se) |
|---|---|---|---|---|
| Frozen rules | 796 | +1.6 (1.4) | 1.1 | -1.5 (1.4) |
| `measured_bars` | 1,166 | +2.4 (1.2) | 1.9 | -0.7 (1.2) |
| `empirical` | 769 | +2.7 (1.4) | 1.9 | -0.4 (1.4) |

- One trade's result swings by about 40 c, so the noise in the average is 40 c / sqrt(trades).
  At about 800 trades an average must reach +2.8 c to be two standard errors above zero.
- The three versions ran on the same events. They are not three independent confirmations.
- July looked much better (+8.4 c and +8.6 c before fees on the two deliveries), but see the next
  section.

## Things that are easy to miss

- **Settlement changed in August.** Between 5 and 20 August Polymarket moved the 5m and 15m
  contracts from the spot price at the end of the window to a 60-second average. The strategy did
  not change; the market did. July results are under the old rule and are not pooled with the rest.
- **Two reference prices.** The 5m and 15m contracts use Chainlink for the line and settlement;
  the 1h contract uses Binance. The difference between the two moves by about $6 over any horizon
  from 30 s to 15 min.
- **The strike is available.** The Gamma API gives it exactly (`eventMetadata.priceToBeat`); it
  matched to $0 on 95 windows, so the error bar has no term for it.
- **The public tape is not a recording.** Quotes there are rebuilt from trades, which flatters
  fills by about 0.9 c, and it has no Chainlink feed (a Binance-based stand-in is used).
- **Tests are written down first.** Each test and its pass criteria go into `results/HISTORY.md`
  and are committed before the data is run. Please keep doing this; it is what makes the numbers
  believable.

## What was done in the last round (13 November)

Asked for after a review of the results: smaller error bars so fewer trades are taken on noise,
a better volatility estimate in the staleness term, and empirical quantiles in place of the
Student-t.

1. **What the error bars are made of.** Almost entirely the allowance for the reference price:
   96% / 89% / 74% of the variance for 5m / 15m / 1h. The price tick is about 3% of a vote.
2. **Measured instead of assumed (`bars.py`).** The reference price's error was measured per
   second on our own recording (124,486 seconds). On our recording the frozen formula was already
   about right ($7.6 against $7.9 measured). On the public tape it was about 1.6 times too wide
   for the 5m and 15m. The 1h contract had no allowance for the Binance-Chainlink difference.
3. **Variant `measured_bars`.** Uses the measured bars, adds the Binance-Chainlink term to the 1h
   contract, and on our own recording moves the last Chainlink price forward with Binance's move
   since its timestamp. Bars are better sized (spread of the disagreement 1.09 / 1.07 against
   0.85 / 0.83, where 1.0 is right). Profit: not supported by the rule written beforehand (t 1.93,
   2 required).
4. **Variant `empirical` (`empirical.py`).** The Student-t is replaced by the stored quantiles of
   the same 90 days of returns. No material difference, as expected: the two shapes price a second
   contract within 1.5 c of each other.
5. **Why a Student-t.** It matches the tails (moves beyond 4 standard deviations: 133 / 41 / 16
   seen, 126 / 42 / 13 predicted, against 1.6 / 0.5 / 0.1 for a normal), it is one parameter per
   horizon instead of 261 numbers, and it behaves in the far tail where the sample has few points.
   `FINDINGS.md`, Phase 5.
6. **The moved-forward Chainlink price, checked on new seconds.** On two hours recorded after the
   measurement its error bar was 35% smaller than the last received price's, and the measured bars
   were the right size (errors beyond 2.5 bars on 1.0% of seconds, 1.2% intended). Neither version
   traded in those two hours, so there is no profit figure for it.

## Open

- **One run is owed and has not been made.** The rule in `results/HISTORY.md`: when our own
  recording holds 30 `measured_bars` trades on events after 09:00 UTC on 11-13, set
  `bars.CHECK_END` to the new end and run
  `python evidence.py variants november-new baseline measured_bars` once. Supported if profit
  before fees is at least two standard errors above zero and above the frozen rules' on the same
  events. At the earlier rate that is around 11-18. Nothing has been run or counted on the
  recording after 11:00 UTC on 11-13.
- **Sample size is the real constraint.** If the true edge is the +1.6 c measured, showing it at
  two standard errors takes about 2,500 trades, three times what the tape holds.
- **Fees.** The taker fee plus fill optimism cost about 3.1 c per trade, more than the measured
  edge in every version. Resting orders were looked at in `maker.py`. Fees were set aside for this
  round on purpose: the question was only whether the signal is real.
- **The realised-volatility vote** of the frozen rules reads 13 to 22% low. Noted, not changed.
- **Other assets.** The recorder can record ETH as a separate copy; it was not started, and the
  decision was to stay on BTC.

## Running it

- The recorder runs on one PC as a scheduled task and writes hourly parquet files. Leave it
  running; analysis only reads from it.
- `python -m unittest discover -s tests` runs the 67 tests.
- `python bars.py measure` prints the measured error bars; `python empirical.py compare` compares
  the shapes; `python evidence.py tape-pooled baseline measured_bars empirical` prints the table
  above.
- Editing `votes.py` changes the cache tag, so cached tables rebuild on the next run. That is
  expected and takes a while.
