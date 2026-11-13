"""The frozen rules on the same contracts for other coins (ETH, SOL, XRP), from the public trade tape.

    set POLY_ASSET=eth                                  (bash: export POLY_ASSET=eth)
    python tape.py fetch 2026-10-13 2026-11-11
    python tape.py build now
    python assets.py run                                # this coin: trades, P&L, checks; saves its trades
    python assets.py pooled                             # all coins that have been run, side by side and pooled

Nothing is changed for the other coins: same rules, same tail parameter (fitted on BTC), same
error bars, same fee. The specification and the decision rule are in results/HISTORY.md
("The same rules on other coins") and were written before any of this data was fetched.
"""
import os
import sys

import numpy as np
import pandas as pd

import backtest
import evidence
import tape
from recorder import config

OUT = os.path.join(config.DATA_DIR, "analysis")


def trades_path(asset):
    return os.path.join(OUT, f"trades_now_{asset}_baseline.parquet")


def run():
    signals, outcomes, _ = evidence.dataset("now")
    print(f"{tape.ASSET.upper()}: {signals.expiry.nunique()} shared expiries, {backtest.fmt_ts(signals.expiry.min())} to "
          f"{backtest.fmt_ts(signals.expiry.max())} UTC")
    voted = signals.n_votes >= 2
    print(f"seconds with 2+ votes: {voted.sum():,} ({voted.mean():.0%} of seconds); signal on {(signals.kind != 'none').sum() / max(voted.sum(), 1):.1%} of them")
    for n in ("5m", "15m", "1h"):
        live = signals.tau <= {"5m": 300, "15m": 900, "1h": 900}[n]
        print(f"  {n}: rebuilt quote present on {signals.loc[live, f'bid_{n}'].notna().mean():.0%} of its seconds")
    backtest.error_bar_check(signals)
    trades, _ = evidence.range_trades("now")
    trades = trades.assign(asset=tape.ASSET)
    trades.to_parquet(trades_path(tape.ASSET))
    lasting = signals[backtest.run_length(signals, signals.side.notna()) == backtest.PERSIST_S]
    moves = backtest.price_moves(signals, lasting, 30)
    if len(moves) > 2:
        print(f"\nPrice move 30 s after a lasting signal: {moves.mid.mean() * 100:+.2f} c (se {moves.mid.std() / np.sqrt(len(moves)) * 100:.2f}) "
              f"against {moves.edge.mean() * 100:.1f} c of edge, {len(moves)} signals")
    show(trades, tape.ASSET.upper())


def show(trades, label):
    for column, kind in (("pnl", "before fees"), ("pnl_net", "after fees and 0.9 c")):
        n, mean, se = evidence.interval(trades[column] * 100)
        print(f"  {label:<14} {kind:<21} {n:>4} trades, won {(trades[column] > 0).mean():.0%}, {mean:+.1f} c per trade (se {se:.1f}), "
              f"95% interval {mean - 1.96 * se:+.1f} to {mean + 1.96 * se:+.1f}")


def pooled():
    parts = {a: pd.read_parquet(trades_path(a)) for a in tape.ASSETS if os.path.exists(trades_path(a))}
    for asset, trades in parts.items():
        show(trades, asset.upper())
    others = [t for a, t in parts.items() if a != "btc"]
    if others:
        print()
        show(pd.concat(others), "ETH+SOL+XRP")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "run":
        run()
    elif command == "pooled":
        pooled()
    else:
        sys.exit(__doc__)
