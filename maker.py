"""The frozen rules' signals, executed with resting orders instead of taking.

    python maker.py            # every dataset that has trades: July tick, and today's rule from the tape

The specification is fixed in results/HISTORY.md ("Resting orders instead of taking") and was
written before the first run. In short: one second after each signal the frozen rules would have
traded, rest one order per leg at the best price on our side (buy at the bid, sell at the ask) for
REST_S seconds. A leg counts as filled only if the market trades through its price ("strict"), at
least SAFETY_S after it was placed. Filled legs are held to expiry. No fee, no rebate.
"""
import glob
import os

import numpy as np
import pandas as pd

import backtest
import evidence
import july
import tape

REST_S = 30
SAFETY_S = 2
EPS = 1e-6
pd.set_option("display.width", 220)


def orders(signals, outcomes, name):
    """One row per leg of each signal: side, resting price, post time, outcome."""
    with evidence.timing(name):
        trades, _ = backtest.simulate(signals, outcomes, backtest.DEFAULT_THRESHOLD)
        delay = backtest.DELAY_S
    quotes = signals.set_index(["expiry", "ts"])
    rows = []
    for t in trades.itertuples():
        post = t.ts + delay * 1000
        q = quotes.loc[(t.expiry, post)]
        for leg, buy in ((t.lo, t.side == "buy"), (t.hi, t.side != "buy")):
            price = q[f"bid_{leg}"] if buy else q[f"ask_{leg}"]
            rows.append({"expiry": t.expiry, "post": post, "leg": leg, "buy": buy, "price": price,
                         "up": outcomes.get((t.expiry, leg), np.nan), "taker_pnl": t.pnl, "taker_net": t.pnl_net})
    return pd.DataFrame(rows)


def fill(order, trades):
    """(strict, at the price) for one resting order. trades: match time in ms, Up price, lift."""
    window = trades[(trades.tm >= order.post + SAFETY_S * 1000) & (trades.tm <= order.post + REST_S * 1000)]
    if order.buy:
        hits = window[~window.lift]
        return bool((hits.px < order.price - EPS).any()), bool((hits.px <= order.price + EPS).any())
    lifts = window[window.lift]
    return bool((lifts.px > order.price + EPS).any()), bool((lifts.px >= order.price - EPS).any())


def tape_trades_for(expiries):
    """{(expiry, leg): trades} from the fetched public tape, with match times (stamps moved back)."""
    lines = tape.now_reference()["lines"]
    lines = lines[(lines.close_ts * 1000).isin(expiries)]
    slug_to = {r.slug: (int(r.close_ts) * 1000, r.length) for r in lines.itertuples()}
    days = sorted(set(pd.to_datetime(np.array(sorted(expiries)) - 1000, unit="ms").strftime("%Y-%m-%d")))
    out = {}
    for day in days:
        for path in glob.glob(os.path.join(tape.TAPE_DIR, f"trades_{day}.parquet")):
            day_trades = pd.read_parquet(path)
            day_trades = day_trades[day_trades.slug.isin(slug_to)]
            for slug, t in day_trades.groupby("slug"):
                u = tape.up_trades(t, tape.API_LAG_S)
                out[slug_to[slug]] = u.assign(tm=u.recv_ts - 1000)
    return out


def july_trades_for(expiries):
    """The same from the July tick files, which carry the exchange's own match time."""
    files = {july.expiry_of(p) * 1000: p for part in ("tick", "holdout") for p in july.event_files(part)}
    out = {}
    for expiry in expiries:
        rows = pd.read_parquet(files[expiry], columns=["rowtype", "leg", "token", "price", "side", "ex_ms"])
        up_token = rows[rows.rowtype == "book"].groupby("leg").token.first()
        for leg, t in rows[(rows.rowtype == "trade") & rows.leg.isin(up_token.index)].groupby("leg"):
            u = tape.up_trades(pd.DataFrame({"ts": (t.ex_ms // 1000).astype("int64"), "price": t.price, "side": t.side,
                                             "outcome_index": (t.token != up_token[leg]).astype(int)}))
            out[(expiry, leg)] = u.assign(tm=u.recv_ts - 1000)
    return out


def run(name, source):
    signals, outcomes, _ = evidence.dataset(name)
    o = orders(signals, outcomes, name).dropna(subset=["price", "up"])
    trades = source(set(o.expiry))
    empty = pd.DataFrame({"tm": [], "px": [], "lift": []}).astype({"lift": bool})
    fills = [fill(r, trades.get((r.expiry, r.leg), empty)) for r in o.itertuples()]
    o["strict"], o["at_price"] = [f[0] for f in fills], [f[1] for f in fills]
    o["leg_pnl"] = np.where(o.buy, o.up - o.price, o.price - o.up)
    return o.assign(data=name)


def per_signal(o, rule):
    """P&L per signal: the sum over its filled legs, zero when nothing filled."""
    pnl = (o.leg_pnl * o[rule]).groupby(o.expiry).sum() * 100
    return pnl


def report(parts, title):
    o = pd.concat(parts)
    print(f"\n{title}: {o.expiry.nunique()} signals, {len(o)} resting orders")
    taker = o.groupby("expiry")[["taker_pnl", "taker_net"]].first() * 100
    print(f"  taking, as before:        {taker.taker_pnl.mean():+.1f} c per signal before fees, {taker.taker_net.mean():+.1f} c after fees")
    rows = []
    for rule, label in (("strict", "strict: traded through us"), ("at_price", "at the price (upper bound)")):
        pnl = per_signal(o, rule)
        filled = o[o[rule]]
        se = pnl.std(ddof=1) / np.sqrt(len(pnl))
        leg = filled.leg_pnl * 100
        rows.append({"fill rule": label, "legs filled": f"{o[rule].mean():.0%}", "signals with a fill": f"{(o[rule].groupby(o.expiry).any()).mean():.0%}",
                     "c per signal": pnl.mean(), "se": se, "95% low": pnl.mean() - 1.96 * se, "95% high": pnl.mean() + 1.96 * se,
                     "c per filled leg": leg.mean(), "leg se": leg.std(ddof=1) / np.sqrt(max(len(leg), 1))})
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    return o


def main():
    print(f"Resting orders on the frozen rules' signals: rest {REST_S} s, fill counted from {SAFETY_S} s after placing.")
    july_parts = [run("july", july_trades_for), run("holdout", july_trades_for)]
    report(july_parts, "July tick recording (old rule; does not decide)")
    today = [run("autumn", tape_trades_for), run("now", tape_trades_for)]
    report(today[:1], "Today's rule, 08-20 to 10-12")
    report(today[1:], "Today's rule, 10-13 to 11-11")
    o = report(today, "ALL OF TODAY'S RULE (the figure that counts is 'c per signal' under the strict rule)")
    o.to_parquet(os.path.join(evidence.config.DATA_DIR, "analysis", "maker_orders_today.parquet"))


if __name__ == "__main__":
    main()
