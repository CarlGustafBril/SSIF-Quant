"""Tests for the trade simulation in backtest.py, on a small hand-made signal table."""
import unittest

import numpy as np
import pandas as pd

import backtest

EXPIRY = 1_000_000_000


def table(edges, side="buy", ask_5m=0.60, bid_15m=0.20):
    """One event, one row per second. lo = 5m, hi = 15m, model value of the range 0.50."""
    n = len(edges)
    return pd.DataFrame({
        "expiry": EXPIRY, "ts": EXPIRY - 300_000 + 1000 * np.arange(n), "tau": 300.0 - np.arange(n),
        "kind": "pair", "outlier": None, "lo": "5m", "hi": "15m",
        "side": [side if e is not None else None for e in edges],
        "edge": [np.nan if e is None else e for e in edges], "model": 0.50,
        "bid_5m": ask_5m - 0.01, "ask_5m": ask_5m, "bid_15m": bid_15m, "ask_15m": bid_15m + 0.01})


class SimulateTest(unittest.TestCase):
    def test_run_length(self):
        t = table([0.05, 0.05, None, 0.05, 0.05, 0.05])
        self.assertEqual(list(backtest.run_length(t, t.side.notna())), [1, 2, 0, 1, 2, 3])

    def test_short_signal_is_not_traded(self):
        t = table([0.10, 0.10, None, None, None])
        trades, _ = backtest.simulate(t, {}, 0.045)
        self.assertEqual(len(trades), 0)

    def test_buy_after_three_seconds_filled_one_second_later(self):
        t = table([None, 0.10, 0.10, 0.10, 0.10, 0.10])
        outcomes = {(EXPIRY, "5m"): 1.0, (EXPIRY, "15m"): 0.0}          # the range pays 1
        trades, _ = backtest.simulate(t, outcomes, 0.045)
        self.assertEqual(len(trades), 1)                                # one position per expiry
        trade = trades.iloc[0]
        self.assertEqual(trade.ts, t.ts[3])                             # third second of the signal
        self.assertAlmostEqual(trade.price, 0.60 - 0.20)                # lower strike's ask minus higher strike's bid
        self.assertAlmostEqual(trade.pnl, 1.0 - 0.40)

    def test_sell_loses_when_the_range_pays(self):
        t = table([0.10] * 5, side="sell", ask_5m=0.71, bid_15m=0.09)   # receive 0.70 - 0.10 = 0.60, model 0.50
        trades, _ = backtest.simulate(t, {(EXPIRY, "5m"): 1.0, (EXPIRY, "15m"): 0.0}, 0.045)
        self.assertAlmostEqual(trades.iloc[0].price, 0.60)
        self.assertAlmostEqual(trades.iloc[0].pnl, 0.60 - 1.0)

    def test_no_trade_if_edge_is_gone_at_the_fill(self):
        t = table([0.10] * 5, ask_5m=0.68)                              # costs 0.48 against a model of 0.50: 2 cents left
        trades, lost = backtest.simulate(t, {}, 0.045)
        self.assertEqual((len(trades), lost), (0, 1))

    def test_unresolved_market_gives_no_pnl(self):
        trades, _ = backtest.simulate(table([0.10] * 5), {}, 0.045)
        self.assertTrue(np.isnan(trades.iloc[0].pnl))
        self.assertEqual(backtest.summarise(trades)["trades"], 0)


class SimulationCheckTest(unittest.TestCase):
    def test_fair_market_is_not_traded_and_mispriced_one_is(self):
        import simulation_check
        rng = np.random.default_rng(7)
        fair = simulation_check.run_scenario(rng, 6, 0.0)
        rich = simulation_check.run_scenario(rng, 6, 0.10)
        self.assertEqual(fair["events traded"], 0)
        self.assertGreaterEqual(rich["events traded"], 4)


if __name__ == "__main__":
    unittest.main()
