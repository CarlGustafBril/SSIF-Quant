"""Tests for the fee, the July clock corrections and the regression used in evidence.py. No data files needed."""
import unittest

import numpy as np
import pandas as pd

import backtest
import evidence
import july
from tests.test_backtest import EXPIRY, table


class FeeTest(unittest.TestCase):
    def test_fee_is_charged_on_both_legs_at_the_fill_prices(self):
        t = table([None, 0.10, 0.10, 0.10, 0.10, 0.10])                 # buys the 5m at 0.60, sells the 15m at 0.20
        trades, _ = backtest.simulate(t, {(EXPIRY, "5m"): 1.0, (EXPIRY, "15m"): 0.0}, 0.045)
        trade = trades.iloc[0]
        self.assertAlmostEqual(trade.fee, 0.07 * (0.60 * 0.40 + 0.20 * 0.80))
        self.assertAlmostEqual(trade.pnl_net, trade.pnl - trade.fee)
        self.assertAlmostEqual(backtest.summarise(trades, "pnl_net")["pnl_cents"], (0.60 - trade.fee) * 100)

    def test_fee_peaks_at_one_and_three_quarter_cents(self):
        self.assertAlmostEqual(backtest.FEE_RATE * 0.5 * 0.5, 0.0175)


class ClockTest(unittest.TestCase):
    def test_tape_offset_is_the_median_of_receive_minus_exchange_time(self):
        rows = pd.DataFrame({"rowtype": ["binance"] * 3 + ["book"], "ts_ms": [10_500, 20_480, 30_520, 5], "ex_ms": [500, 500, 500, np.nan]})
        rows["ex_ms"] = rows.ts_ms - [10_000, 9_980, 10_020, 0]
        self.assertEqual(july.clock_offset_ms(rows), 10_000)

    def test_poll_clock_follows_the_tape_and_never_goes_below_half_a_second(self):
        self.assertAlmostEqual(float(july.rest_clock_offset_ms(july.CLOCK_REF_MS)), 9225.0)
        one_day_on = float(july.rest_clock_offset_ms(july.CLOCK_REF_MS + 86_400_000))
        self.assertAlmostEqual(one_day_on - 9225.0, 1045.0, places=3)
        self.assertEqual(float(july.rest_clock_offset_ms(july.CLOCK_REF_MS - 30 * 86_400_000)), 500.0)

    def test_first_and_second_delivery_do_not_overlap(self):
        self.assertEqual(pd.Timestamp(july.SEEN_UNTIL, unit="s").strftime("%Y-%m-%d %H:%M"), "2026-07-18 20:45")


class SlopeTest(unittest.TestCase):
    def test_slope_recovers_a_known_beta_and_gives_a_sensible_error(self):
        rng = np.random.default_rng(3)
        events = np.repeat(np.arange(200), 20)
        gap = rng.normal(0, 0.05, len(events))
        d = pd.DataFrame({"expiry": events, "gap": gap, "y": 0.4 * gap + rng.normal(0, 0.02, len(events))})
        out = evidence.slope(d, "y")
        self.assertAlmostEqual(out["beta"], 0.4, delta=4 * out["se"])
        self.assertLess(out["se"], 0.02)
        self.assertEqual(out["events"], 200)

    def test_too_little_data_gives_no_slope(self):
        d = pd.DataFrame({"expiry": [1, 1, 2], "gap": [0.1, 0.2, 0.3], "y": [0.0, 0.1, 0.2]})
        self.assertTrue(np.isnan(evidence.slope(d, "y")["beta"]))


if __name__ == "__main__":
    unittest.main()
