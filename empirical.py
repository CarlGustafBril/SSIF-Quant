"""Empirical return distribution, as an alternative to the Student-t of the frozen rules.

    python empirical.py build       # write empirical.json from the same 90 days nu.json was fitted on
    python empirical.py compare     # how the empirical shape differs from the fitted Student-t

The sample is the one fit_nu.py fits: for each horizon (5, 15, 60 minutes), the log return over
every clock-aligned window divided by the realised volatility of the 60 minutes before it. The
Student-t keeps one number from that sample (nu). Here the sample's own quantiles are kept:

  * made symmetric (the sample and its mirror image), because the model has no drift;
  * stored as the quantiles of |z| on a fixed grid of tail probabilities, read by interpolation;
  * in the sample's own units. z is "return / what realised volatility predicted", so a width of 1
    is realised volatility itself and the realised-vol vote needs no conversion (sd_factor = 1).

Used by the variant "empirical" in variants.py through votes.shape_* (see votes.py).
"""
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

import fit_nu
import votes

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "empirical.json")
FIRST_DAY, LAST_DAY = "2026-08-13", "2026-11-11"          # the days nu.json was fitted on (its 90 days and one spare)
DAYS = 90
P_GRID = np.concatenate([np.linspace(0.5, 0.05, 181), np.geomspace(0.05, 0.0005, 81)[1:]])     # tail probabilities, falling
SLOPE_HALF_WIDTH = 0.01                                    # the quantile's slope is measured over at least +-1 cent of price


class Shape:
    """Symmetric distribution given by quantiles of |z| at the tail probabilities P_GRID."""

    def __init__(self, p, q):
        self.p_down, self.q_up = np.asarray(p, dtype=float), np.asarray(q, dtype=float)      # p falls, q rises
        self.p_up, self.q_down = self.p_down[::-1], self.q_up[::-1]
        self.sd_factor = 1.0

    def isf(self, p):
        """x with P(Z >= x) = p."""
        if p > 0.5:
            return -self.isf(1 - p)
        return float(np.interp(p, self.p_up, self.q_down))

    def sf(self, x):
        """P(Z >= x). Beyond the last stored quantile it stays at the smallest stored probability."""
        if x < 0:
            return 1 - self.sf(-x)
        return float(np.interp(x, self.q_up, self.p_down))

    def quote_err(self, mid, tick):
        """Error of isf(mid) when the price is known to half a tick: half a tick times the quantile's slope."""
        h = max(tick / 2, SLOPE_HALF_WIDTH)
        lo, hi = max(mid - h, self.p_up[0]), min(mid + h, 1 - self.p_up[0])
        return abs(self.isf(lo) - self.isf(hi)) / (hi - lo) * tick / 2


def sample(horizon_min):
    """The standardised returns fit_nu.py fits, for the fixed 90 days."""
    days = pd.date_range(FIRST_DAY, LAST_DAY).strftime("%Y-%m-%d")
    candles = pd.concat([fit_nu.download_day(day) for day in days]).drop_duplicates("open_ts").set_index("open_ts").close.sort_index()
    candles = candles.iloc[-DAYS * 1440:]
    close = candles.reindex(range(candles.index[0], candles.index[-1] + 60_000, 60_000))
    return fit_nu.standardised_returns(close, horizon_min)


def build():
    out = {"days": DAYS, "first_day": FIRST_DAY, "last_day": LAST_DAY, "p": [round(float(p), 6) for p in P_GRID], "q_by_horizon_minutes": {}}
    for h in fit_nu.HORIZONS_MIN:
        z = np.abs(sample(h))
        out["q_by_horizon_minutes"][str(h)] = [round(float(q), 5) for q in np.quantile(z, 1 - 2 * P_GRID)]
        print(f"{h:>3} m: {len(z):,} windows")
    with open(FILE, "w") as f:
        json.dump(out, f)
    print(f"Saved {FILE}")


def load_shapes():
    """{horizon in minutes: Shape}, the counterpart of votes.load_nu()."""
    with open(FILE) as f:
        data = json.load(f)
    return {int(h): Shape(data["p"], q) for h, q in data["q_by_horizon_minutes"].items()}


def compare():
    """Where the two shapes differ. Only ratios of quantiles matter: the width is fitted to each price."""
    shapes, nus = load_shapes(), votes.load_nu()
    print("A contract priced p implies a width  c = x / q(p). Two contracts are compared through the RATIO of their q.")
    print("Shown: q(p) / q(0.30) for each shape. Equal ratios mean identical votes and identical trades.\n")
    prices = (0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.45)
    rows = []
    for h in sorted(shapes):
        for name, isf in (("Student-t", lambda p: stats.t.isf(p, nus[h])), ("empirical", shapes[h].isf), ("normal", stats.norm.isf)):
            rows.append({"horizon": f"{h} m", "shape": name if name != "Student-t" else f"Student-t, nu {nus[h]:.2f}",
                         **{f"p={p:.2f}": isf(p) / isf(0.30) for p in prices}})
    pd.set_option("display.width", 200)
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    print("\nWidth matching realised volatility (the realised-vol vote), as a multiple of it:")
    for h in sorted(shapes):
        z = sample(h)
        nu, _, scale = stats.t.fit(z, floc=0)
        print(f"  {h:>3} m: frozen rules sqrt((nu-2)/nu) = {votes.shape_sd_factor(nus[h]):.3f}; scale of the fitted t = {scale:.3f}; "
              f"empirical shape = 1 by construction; sample sd of z = {z.std():.3f}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "build":
        build()
    elif command == "compare":
        compare()
    else:
        sys.exit(__doc__)
