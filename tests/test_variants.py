"""Tests for the variant hooks: shared stale-price error, bias scaling and the three-contract rule."""
import math
import unittest

import votes
import variants

NU = 3.954
SPOT = 74_030.0
SD = 0.30 / math.sqrt(365 * 86400)
STALE = SPOT * SD * math.sqrt(2.1)


def leg(name, line, bid, ask, tau=200):
    return {"name": name, "spot": SPOT, "line": line, "bid": bid, "ask": ask, "tick": 0.01, "line_err": STALE,
            "tau": tau, "w": 60, "stale": STALE, "feed": "chainlink"}


class SharedStaleTest(unittest.TestCase):
    def pair(self, legs):
        a, b = sorted((votes.leg_vote(l, NU) for l in legs), key=lambda v: v["x"])
        return a, b, variants.shared_pair_sigma(NU)(a, b), math.hypot(a["sv"], b["sv"])

    def test_same_side_is_tighter_than_independent(self):
        _, _, shared, independent = self.pair([leg("5m", 74_050, 0.30, 0.31), leg("15m", 74_120, 0.13, 0.14)])
        self.assertLess(shared, independent)

    def test_straddle_is_wider_than_independent(self):
        _, _, shared, independent = self.pair([leg("5m", 74_010, 0.65, 0.66), leg("15m", 74_060, 0.25, 0.26)])
        self.assertGreater(shared, independent)

    def test_different_feeds_stay_independent(self):
        hourly = dict(leg("1h", 74_120, 0.13, 0.14), w=0, feed="binance")
        a, b, shared, independent = self.pair([leg("5m", 74_050, 0.30, 0.31), hourly])
        self.assertAlmostEqual(shared, independent)


class BiasAndRulesTest(unittest.TestCase):
    def test_scale_is_undone_when_valuing(self):
        raw = votes.leg_vote(leg("15m", 74_120, 0.13, 0.14), NU)
        scaled = variants.bias_adjust({"15m": math.log(1.2)})([raw])[0]
        self.assertAlmostEqual(scaled["v"] * 1.2, raw["v"])
        # valuing the leg at its own (scaled) vote gives back its own market mid
        self.assertAlmostEqual(votes.model_prob(scaled, scaled["v"], NU), raw["mid"], places=6)

    def test_defaults_are_the_frozen_rules(self):
        legs = [leg("5m", 74_050, 0.30, 0.31), leg("15m", 74_120, 0.13, 0.14)]
        frozen, hooked = votes.signal(legs, SD, NU), votes.signal(legs, SD, NU, adjust=None, pair_sigma=None, min_votes=2)
        self.assertEqual((frozen["kind"], frozen["side"], frozen["edge"]), (hooked["kind"], hooked["side"], hooked["edge"]))
        self.assertEqual(frozen["kind"], "pair")

    def test_three_only_ignores_pairs(self):
        legs = [leg("5m", 74_050, 0.30, 0.31), leg("15m", 74_120, 0.13, 0.14)]
        self.assertEqual(votes.signal(legs, SD, NU, min_votes=3)["kind"], "none")


if __name__ == "__main__":
    unittest.main()
