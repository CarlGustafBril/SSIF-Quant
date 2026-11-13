"""The frozen rules on the same contracts for other coins (ETH, SOL, XRP), from the public trade tape.

    set POLY_ASSET=eth                                  (bash: export POLY_ASSET=eth)
    python tape.py fetch 2026-10-13 2026-11-11
    python tape.py build now
    python assets.py run                                # this coin: trades, P&L, checks; saves its trades
    python assets.py fills                              # this coin: the same trades paid for at the next real trades
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


FILL_WITHIN_S = 10


def real_fills(period="now"):
    """The frozen rules' trades, paid for at prices that actually traded instead of at rebuilt quotes.

    A rebuilt quote can be up to 15 s old. Where trading is thin, an old quote next to a fresh
    reference price looks like a mispricing, and "filling" at it is buying at a price that is gone.
    Here each leg is filled at the next real taker trade on our side (someone paid that ask, or hit
    that bid), from one second after the entry and within FILL_WITHIN_S seconds. Both legs must
    fill. Shown twice: every such trade, and only those where the model's edge is still at least
    the threshold at the prices actually paid, which is what a trader watching the book would do.
    """
    import maker
    signals, outcomes, _ = evidence.dataset(period)
    with evidence.timing(period):
        trades, _ = backtest.simulate(signals, outcomes, backtest.DEFAULT_THRESHOLD)
        delay = backtest.DELAY_S
    trades = trades.dropna(subset=["pnl"])
    tape_trades = maker.tape_trades_for(set(trades.expiry))
    rows = []
    for t in trades.itertuples():
        entry = t.ts + delay * 1000
        prices = {}
        for leg, buy in ((t.lo, t.side == "buy"), (t.hi, t.side != "buy")):
            tt = tape_trades.get((t.expiry, leg))
            if tt is None:
                break
            w = tt[(tt.tm >= entry + 1000) & (tt.tm <= entry + FILL_WITHIN_S * 1000) & (tt.lift == buy)]
            if w.empty:
                break
            prices[leg] = float(w.sort_values("tm").px.iloc[0])
        if len(prices) < 2:
            rows.append({"expiry": t.expiry, "filled": False})
            continue
        price = prices[t.lo] - prices[t.hi]
        model = t.price + t.edge_at_entry if t.side == "buy" else t.price - t.edge_at_entry
        edge = model - price if t.side == "buy" else price - model
        pnl = t.payoff - price if t.side == "buy" else price - t.payoff
        fee = backtest.FEE_RATE * sum(v * (1 - v) for v in prices.values())
        rows.append({"expiry": t.expiry, "filled": True, "pnl": pnl, "pnl_net": pnl - fee, "edge_real": edge,
                     "quoted_pnl": t.pnl, "slip": (t.pnl - pnl)})
    out = pd.DataFrame(rows).assign(asset=tape.ASSET, period=period)
    out.to_parquet(os.path.join(OUT, f"fills_{period}_{tape.ASSET}.parquet"))
    return out


def show_fills(out, label):
    n = len(out)
    f = out[out.filled]
    keep = f[f.edge_real >= backtest.DEFAULT_THRESHOLD]
    print(f"{label}: {n} trades at rebuilt quotes; both legs traded for real within {FILL_WITHIN_S} s on {len(f)} ({len(f) / max(n, 1):.0%})")
    if len(f) > 2:
        print(f"  on those, the rebuilt quote was better than the real prices by {f.slip.mean() * 100:+.1f} c per trade")
    for part, name in ((f, "filled at real trades, all"), (keep, f"same, edge still >= {backtest.DEFAULT_THRESHOLD * 100:.1f} c at real prices")):
        for column, kind in (("pnl", "before fees"), ("pnl_net", "after fees")):
            if len(part) > 2:
                k, mean, se = evidence.interval(part[column] * 100)
                print(f"  {name:<46} {kind:<12} {k:>4} trades, won {(part[column] > 0).mean():.0%}, {mean:+.1f} c (se {se:.1f}), "
                      f"95% {mean - 1.96 * se:+.1f} to {mean + 1.96 * se:+.1f}")


def pooled_fills():
    import glob
    parts = [pd.read_parquet(f) for f in sorted(glob.glob(os.path.join(OUT, "fills_*.parquet")))]
    if not parts:
        return
    everything = pd.concat(parts)
    print()
    for (asset, period), part in everything.groupby(["asset", "period"]):
        show_fills(part, f"{asset.upper()} {period}")
    others = everything[everything.asset != "btc"]
    if len(others):
        show_fills(others, "ETH+SOL+XRP")
    show_fills(everything[everything.asset == "btc"], "BTC, all of today's rule")
    show_fills(everything, "ALL COINS")


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
        pooled_fills()
    elif command == "fills":
        for period in (sys.argv[2:] or ["now"]):
            show_fills(real_fills(period), f"{tape.ASSET.upper()} {period}")
    else:
        sys.exit(__doc__)
