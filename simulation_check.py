"""Does the engine find a mispricing when there is one, and stay quiet when there is none?

    python simulation_check.py            # 150 simulated events per scenario, about 2 minutes
    python simulation_check.py 40         # fewer events, quicker

Real data cannot answer this yet (too few trades), so this builds markets where the truth is known.
Each simulated event has three contracts on one expiry, quoted one tick wide around the model's
own fair price. In the "fair" scenario nothing is mispriced. In the others the 15m contract is
quoted a fixed number of cents too high for the whole event. The same code as the real backtest
(votes.signal and backtest.simulate) then decides what to trade, and the outcome is drawn from the
model's distribution at the moment of the trade.

What to look for:
  fair         few signals, almost no trades
  mispriced    the 15m named as the outlier, and P&L per trade close to the edge the model saw at the fill
               (within its interval; single trades win or lose tens of cents)
"""
import math
import sys

import numpy as np
import pandas as pd
from scipy import stats

import backtest
import votes

NU = 4.0
SECONDS = 240                    # each event is watched from 300 s to 60 s before expiry
VOL_YEAR = 0.5                   # true volatility
TICK = 0.01
THRESHOLD = backtest.DEFAULT_THRESHOLD
LEG_NAMES = ("5m", "15m", "1h")


def simulate_event(rng, expiry, shift_15m):
    """Per-second table for one event, in the format backtest.simulate expects, plus the legs' lines."""
    sd = VOL_YEAR / math.sqrt(365 * 86400)                       # true sd per sqrt-second
    width = sd * math.sqrt((NU - 2) / NU)                        # the t scale with that sd
    spot = 100_000 * np.exp(np.cumsum(rng.normal(0, sd, SECONDS)))
    lines = {"5m": 100_000 * math.exp(rng.normal(0, 0.0004)),
             "15m": 100_000 * math.exp(rng.normal(0, 0.0010)),
             "1h": 100_000 * math.exp(rng.normal(0, 0.0015))}
    rows = []
    for n in range(SECONDS):
        tau = 300 - n
        legs = []
        for name in LEG_NAMES:
            fair = float(stats.t.sf(math.log(lines[name] / spot[n]) / (width * math.sqrt(tau)), NU))
            mid = round((fair + (shift_15m if name == "15m" else 0.0)) / TICK) * TICK + TICK / 2
            legs.append({"name": name, "spot": spot[n], "line": lines[name], "bid": mid - TICK / 2, "ask": mid + TICK / 2,
                         "tick": TICK, "line_err": 0.0, "tau": tau, "w": 0})
        s = votes.signal(legs, sd, NU)
        row = {"expiry": expiry, "ts": expiry - tau * 1000, "tau": tau, "spot": spot[n], "kind": s["kind"],
               "n_votes": s["n_votes"], "outlier": s["outlier"], "lo": s["lo"], "hi": s["hi"], "side": s["side"],
               "edge": s["edge"], "model": s["model"]}
        for leg in legs:
            row[f"bid_{leg['name']}"], row[f"ask_{leg['name']}"] = leg["bid"], leg["ask"]
        rows.append(row)
    return pd.DataFrame(rows), lines, width


def run_scenario(rng, events, shift_15m):
    tables, outcomes, all_trades = [], {}, []
    for k in range(events):
        expiry = (k + 1) * 10_000_000
        table, lines, width = simulate_event(rng, expiry, shift_15m)
        trades, _ = backtest.simulate(table, {}, THRESHOLD)
        if len(trades):                                          # settle from the moment of the trade
            at = table[table.ts == trades.iloc[0].ts + backtest.DELAY_S * 1000].iloc[0]
            final = at.spot * math.exp(width * math.sqrt(at.tau) * rng.standard_t(NU))
            for name in LEG_NAMES:
                outcomes[(expiry, name)] = float(final >= lines[name])
        tables.append(table)
    signals = pd.concat(tables, ignore_index=True)
    trades, _ = backtest.simulate(signals, outcomes, THRESHOLD)
    voted = signals[signals.n_votes >= 2]
    three = signals[signals.n_votes >= 3]
    s = backtest.summarise(trades)
    named = three[three.kind == "outlier"]
    return {"15m quoted too high by": f"{shift_15m * 100:.0f} c" if shift_15m else "0 (fair)",
            "events": events, "signal rate": f"{(voted.kind != 'none').mean():.1%}",
            "3-leg seconds naming 15m": f"{(named.outlier == '15m').sum() / max(len(three), 1):.0%}",
            "naming another leg": f"{(named.outlier != '15m').sum() / max(len(three), 1):.1%}",
            "events traded": s["trades"],
            "edge at fill (c)": f"{trades.edge_at_entry.mean() * 100:+.1f}" if s["trades"] else "",
            "P&L per trade (c)": f"{s['pnl_cents']:+.1f}" if s["trades"] else "",
            "95% interval": f"[{s['ci_low']:+.1f}, {s['ci_high']:+.1f}]" if s["trades"] > 1 else ""}


def main():
    events = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    rng = np.random.default_rng(1)
    rows = [run_scenario(rng, events, shift) for shift in (0.0, 0.03, 0.06, 0.10)]
    print(f"Simulated markets: nu = {NU:g}, volatility {VOL_YEAR:.0%} a year, spread 1 tick, "
          f"threshold {THRESHOLD * 100:.1f} c, signal must hold {backtest.PERSIST_S} s\n")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
