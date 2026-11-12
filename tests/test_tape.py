"""Tests for the quotes rebuilt from the public trade tape (tape.py). No data files needed."""
import unittest

import numpy as np
import pandas as pd

import tape


def trades(rows):
    return pd.DataFrame(rows, columns=["ts", "price", "side", "outcome_index"])


class QuotesFromTradesTest(unittest.TestCase):
    def test_a_buy_of_up_sets_the_ask_and_a_sell_the_bid_one_second_later(self):
        t = trades([(100, 0.56, "BUY", 0), (100, 0.55, "SELL", 0)])
        bid, ask, age = tape.quotes_from_trades(t, np.array([100_000, 101_000, 105_000], dtype="int64"))
        self.assertTrue(np.isnan(bid[0]) and np.isnan(ask[0]))           # not known in its own second
        self.assertEqual((bid[1], ask[1]), (0.55, 0.56))
        self.assertEqual(age[2], 4000)

    def test_trades_in_the_down_token_are_mirrored(self):
        t = trades([(100, 0.40, "BUY", 1), (100, 0.38, "SELL", 1)])     # buying Down at 0.40 is selling Up at 0.60
        bid, ask, _ = tape.quotes_from_trades(t, np.array([101_000], dtype="int64"))
        self.assertAlmostEqual(bid[0], 0.60)
        self.assertAlmostEqual(ask[0], 0.62)

    def test_one_fresh_side_puts_the_other_a_tick_away(self):
        t = trades([(100, 0.30, "BUY", 0)])
        bid, ask, _ = tape.quotes_from_trades(t, np.array([101_000], dtype="int64"))
        self.assertAlmostEqual(ask[0], 0.30)
        self.assertAlmostEqual(bid[0], 0.29)

    def test_crossed_sides_follow_the_fresher_trade(self):
        t = trades([(100, 0.50, "BUY", 0), (104, 0.60, "SELL", 0)])     # the price moved up after the buy
        bid, ask, _ = tape.quotes_from_trades(t, np.array([106_000], dtype="int64"))
        self.assertAlmostEqual(bid[0], 0.60)
        self.assertAlmostEqual(ask[0], 0.61)

    def test_old_trades_give_no_quote(self):
        t = trades([(100, 0.50, "BUY", 0)])
        bid, ask, _ = tape.quotes_from_trades(t, np.array([(101 + tape.MAX_TRADE_AGE_S + 1) * 1000], dtype="int64"))
        self.assertTrue(np.isnan(bid[0]) and np.isnan(ask[0]))

    def test_the_public_tapes_late_stamp_is_moved_back(self):
        t = trades([(100, 0.56, "BUY", 0)])
        grid = np.array([97_000, 98_000], dtype="int64")
        _, ask, _ = tape.quotes_from_trades(t, grid, lag_s=4)             # matched at about 96, known from 97
        self.assertAlmostEqual(ask[0], 0.56)


class SlugTest(unittest.TestCase):
    def test_hourly_slug_uses_new_york_time(self):
        open_s = int(pd.Timestamp("2026-11-12T21:00", tz="UTC").timestamp())       # 4 pm in New York (winter time)
        self.assertEqual(tape.hourly_slug(open_s), "bitcoin-up-or-down-november-12-2026-4pm-et")
        summer = int(pd.Timestamp("2026-07-11T16:00", tz="UTC").timestamp())       # noon in New York (summer time)
        self.assertEqual(tape.hourly_slug(summer), "bitcoin-up-or-down-july-11-2026-12pm-et")

    def test_contract_list_has_one_5m_and_15m_per_quarter_hour_and_a_1h_per_hour(self):
        main, extra = tape.contracts_for("2026-10-13", "2026-10-13")
        self.assertEqual(main.length.value_counts().to_dict(), {"15m": 96, "5m": 96, "1h": 24})
        self.assertEqual(len(extra), 192)


if __name__ == "__main__":
    unittest.main()
