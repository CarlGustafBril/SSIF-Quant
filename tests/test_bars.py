"""Tests for the measured error bars, the moved-forward Chainlink price and the empirical shape."""
import math
import unittest

import numpy as np
from scipy import stats

import bars
import empirical
import variants
import votes

NU = 3.954
SPOT = 74_030.0
SD = 0.30 / math.sqrt(365 * 86400)


def shape_from_t(nu=NU, scale=1.0):
    """An empirical.Shape holding the exact quantiles of a Student-t, so the two can be compared."""
    return empirical.Shape(empirical.P_GRID, stats.t.isf(empirical.P_GRID, nu) * scale)


def leg(name, line, bid, ask, tau=200, line_err=5.0):
    return {"name": name, "spot": SPOT, "line": line, "bid": bid, "ask": ask, "tick": 0.01, "line_err": line_err, "tau": tau, "w": 60}


class MeasuringTest(unittest.TestCase):
    def test_bar_size_of_a_normal_error_is_its_sd(self):
        errors = np.random.default_rng(1).normal(0, 4.0, 400_000)
        self.assertAlmostEqual(bars.bar_size(errors), 4.0, delta=0.1)

    def test_bar_size_is_wider_than_typical_for_fat_tails(self):
        errors = stats.t.rvs(2.5, size=200_000, random_state=2)
        self.assertGreater(bars.bar_size(errors), bars.robust_sd(errors))

    def test_fit_recovers_fixed_and_proportional_parts(self):
        rng = np.random.default_rng(3)
        unit = rng.uniform(2, 14, 300_000)
        errors = rng.normal(0, 1, len(unit)) * np.hypot(3.0, 0.8 * unit)
        a, k, _ = bars.fit(errors, unit)
        self.assertAlmostEqual(a, 3.0, delta=0.4)
        self.assertAlmostEqual(k, 0.8, delta=0.05)

    def test_spot_error_combines_both_parts(self):
        a, k = bars.MEASURED["nowcast"]
        self.assertAlmostEqual(bars.spot_error("nowcast", SPOT, SD), math.hypot(a, k * SPOT * SD))

    def test_hourly_contract_carries_the_gap(self):
        plain = variants.measured_dollars("binance", "15m", SPOT, SD, SD, 0.0)
        hourly = variants.measured_dollars("binance", "1h", SPOT, SD, SD, 0.0)
        self.assertAlmostEqual(hourly, math.hypot(plain, bars.spot_error("gap", SPOT, SD)))

    def test_no_volatility_means_no_bar(self):
        self.assertTrue(math.isnan(variants.measured_dollars("nowcast", "5m", SPOT, float("nan"), float("nan"), 0.0)))


class MovedForwardTest(unittest.TestCase):
    def test_follows_binance_since_the_stamp(self):
        out = bars.moved_forward([74_000.0], [1000], [74_110.0], [3000], [74_100.0])
        self.assertAlmostEqual(out[0], 74_000.0 * 74_110.0 / 74_100.0)

    def test_unchanged_when_binance_is_not_newer(self):
        self.assertEqual(bars.moved_forward([74_000.0], [3000], [74_110.0], [3000], [74_100.0])[0], 74_000.0)

    def test_unchanged_when_binance_then_is_unknown(self):
        self.assertEqual(bars.moved_forward([74_000.0], [1000], [74_110.0], [3000], [np.nan])[0], 74_000.0)


class ShapeTest(unittest.TestCase):
    def test_round_trip_and_symmetry(self):
        shape = shape_from_t()
        for p in (0.03, 0.2, 0.45, 0.5, 0.7, 0.97):
            self.assertAlmostEqual(shape.sf(shape.isf(p)), p, places=6)
            self.assertAlmostEqual(shape.isf(p), -shape.isf(1 - p), places=9)

    def test_matches_student_t_between_grid_points(self):
        shape = shape_from_t()
        for p in (0.021, 0.113, 0.277, 0.431):
            self.assertAlmostEqual(shape.isf(p), stats.t.isf(p, NU), delta=0.01)

    def test_quote_error_matches_student_t(self):
        shape = shape_from_t()
        for mid in (0.1, 0.3, 0.6):
            q = stats.t.isf(mid, NU)
            self.assertAlmostEqual(shape.quote_err(mid, 0.01), votes.shape_quote_err(mid, q, 0.01, NU), delta=0.05 * votes.shape_quote_err(mid, q, 0.01, NU))

    def test_frozen_rules_unchanged_for_a_number(self):
        vote = votes.leg_vote(leg("15m", 74_120, 0.13, 0.14), NU)
        self.assertAlmostEqual(vote["q"], float(stats.t.isf(0.135, NU)))
        self.assertAlmostEqual(votes.anchor_vote(SD, NU)["v"], SD * math.sqrt((NU - 2) / NU))
        self.assertIsNone(votes.anchor_vote(SD, 2.0))

    def test_signal_is_the_same_for_a_t_shaped_empirical_shape(self):
        legs = [leg("5m", 74_050, 0.30, 0.31, line_err=0.5), leg("15m", 74_120, 0.27, 0.28, line_err=0.5)]
        frozen = votes.signal(legs, SD, NU)
        self.assertEqual(frozen["kind"], "pair")
        self.assertIsNotNone(frozen["side"])
        other = votes.signal(legs, SD * math.sqrt((NU - 2) / NU), shape_from_t())     # the shape's anchor is sd itself
        self.assertEqual(frozen["kind"], other["kind"])
        self.assertEqual(frozen["side"], other["side"])
        self.assertAlmostEqual(frozen["edge"], other["edge"], delta=0.002)

    def test_scale_of_the_shape_does_not_change_prices(self):
        a = votes.leg_vote(leg("15m", 74_120, 0.13, 0.14), shape_from_t(scale=1.0))
        b = votes.leg_vote(leg("15m", 74_120, 0.13, 0.14), shape_from_t(scale=2.5))
        self.assertAlmostEqual(votes.model_prob(a, a["v"], shape_from_t(scale=1.0)), votes.model_prob(b, b["v"], shape_from_t(scale=2.5)), places=6)

    def test_saved_shapes_load(self):
        shapes = empirical.load_shapes()
        self.assertEqual(sorted(shapes), [5, 15, 60])
        for shape in shapes.values():
            self.assertTrue(np.all(np.diff(shape.q_up) >= 0))
            self.assertAlmostEqual(shape.isf(0.5), 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
