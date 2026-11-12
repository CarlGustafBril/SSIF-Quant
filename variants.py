"""Strategy variants: the frozen rules and alternatives, evaluated side by side, every run logged.

    python variants.py                          # every variant
    python variants.py baseline shared_stale    # only these

Each variant re-decides every second from the per-second tables backtest.py caches (prices, lines,
spot, its age, volatility). So every variant sees exactly the same inputs, and none reloads raw
data. The frozen rules are the variant "baseline"; the run checks that it reproduces the cached
decisions before trusting the others.

Every variant is evaluated twice: on everything recorded, and on events expiring after the rule
freeze (FREEZE_UTC), which no rule was designed on. Nothing is tuned on P&L: the edge threshold is
the fixed 4.5 cents for all variants.

Each run appends one row per variant and period to results/variant_runs.csv, with the git commit
that produced it, and rewrites results/VARIANTS.md with the definitions and the latest results.
"""
import json
import math
import os
import subprocess
import sys

import numpy as np
import pandas as pd
from scipy import stats

import backtest
import votes
from recorder.reader import fmt_ts, load
from recorder.store import now_ms

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")
RUNS_CSV = os.path.join(RESULTS_DIR, "variant_runs.csv")
SUMMARY_MD = os.path.join(RESULTS_DIR, "VARIANTS.md")
FREEZE_UTC = "2026-11-12T11:00"
BIAS_MIN_SECONDS = 300              # seconds of earlier data a pair needs before its offset is used
MOVE_HORIZON_S = 30

DEFAULTS = {"z_limit": votes.Z_LIMIT, "staleness": "independent", "min_votes": 2, "bias": False,
            "exit_after_s": None}

VARIANTS = {
    "baseline": {
        "desc": "The frozen rules (2026-11-12 11:00 UTC): staleness in the error bars, 2.5-bar limit, "
                "two or three contracts, held to expiry."},
    "brief_bars": {
        "staleness": "off",
        "desc": "The brief's error bars: tick and line error only, no allowance for our stale spot price."},
    "limit_4": {
        "z_limit": 4.0,
        "desc": "Disagreement limit 4.0 error bars, the lowest limit that signals on at most 1% of seconds."},
    "three_only": {
        "min_votes": 3,
        "desc": "Trade only when three contracts vote, so the mispriced one can be named."},
    "shared_stale": {
        "staleness": "shared",
        "desc": "The 5m and 15m share one Chainlink price, so a stale price moves both votes at once. "
                "Their combined error bar uses that shared error instead of treating it as independent."},
    "bias_corrected": {
        "bias": True,
        "desc": "Each contract's vote is divided by its typical width relative to the 5m, measured on "
                "earlier events only (the 15m prices about 15% wider than the 1h)."},
    "bias_shared": {
        "bias": True, "staleness": "shared",
        "desc": "bias_corrected and shared_stale together."},
    "exit_30s": {
        "exit_after_s": 30,
        "desc": "Baseline entries, but sold back at bid/ask 30 seconds after entry instead of held to expiry."},
}


# ============================================================================ re-deciding one second
def stale_dollars(spot, age_s, sd, sd_fast):
    """The staleness allowance exactly as in votes.event_table: spot x volatility x sqrt(age)."""
    sd_now = np.nanmax([sd, sd_fast]) if sd > 0 or sd_fast > 0 else np.nan
    return spot * sd_now * math.sqrt(age_s)


def legs_for_row(r, names, staleness):
    legs = []
    for n in names:
        spot, age_s = r[f"spot_{n}"], max(r[f"feed_age_{n}"], 0) / 1000
        stale = 0.0
        if staleness != "off":
            stale = stale_dollars(spot, age_s, r["sd"], r["sd_fast"])
            if not stale >= 0:                      # no volatility figure: no vote, as in the frozen rules
                spot, stale = np.nan, 0.0
        legs.append({"name": n, "spot": spot, "line": r[f"line_{n}"], "bid": r[f"bid_{n}"], "ask": r[f"ask_{n}"],
                     "tick": r[f"tick_{n}"], "line_err": math.hypot(votes.LINE_ERR[n], stale), "tau": r["tau"],
                     "w": votes.AVG_WINDOW_S[n], "stale": stale,
                     "feed": "chainlink" if votes.AVG_WINDOW_S[n] else "binance"})
    return legs


def shared_pair_sigma(nu):
    """Combined error bar of two votes. When both use the Chainlink price, the stale-price part is shared.

    A spot error dS moves leg i's vote by -(dS / S) / (q_i * sqrt(te_i)). For two legs on the same
    price those moves are the same dS, so they partly cancel when both lines are on one side of
    spot and add up when the lines straddle it. The tick part stays independent.
    """
    def sigma(a, b):
        if not (a["feed"] == b["feed"] == "chainlink"):
            return math.hypot(a["sv"], b["sv"])
        parts = []
        for v in (a, b):
            scale = v.get("scale", 1.0)
            tick_q = 0.5 * v["tick"] / float(stats.t.pdf(v["q"], nu))
            tick_v = v["c"] * tick_q / abs(v["q"]) / math.sqrt(v["te"]) / scale
            move_v = -(v["stale"] / v["spot"]) / (v["q"] * math.sqrt(v["te"])) / scale
            parts.append((tick_v, move_v))
        (ta, ma), (tb, mb) = parts
        return math.sqrt(ta ** 2 + tb ** 2 + (ma - mb) ** 2)
    return sigma


def bias_adjust(offsets):
    """Rescale each vote into 5m-equivalent units: v / scale, where scale = exp(offset of that contract)."""
    def adjust(vs):
        out = []
        for v in vs:
            scale = math.exp(offsets.get(v["name"], 0.0))
            out.append(dict(v, v=v["v"] / scale, sv=v["sv"] / scale, scale=scale))
        return out
    return adjust


def bias_offsets(history):
    """Typical log width of each contract relative to the 5m, from earlier seconds only.

    history: per-second v_5m, v_15m, v_1h from events that expired before this one started.
    The 1h is linked through the 15m (the 5m and 1h rarely vote together).
    """
    offsets = {}
    def median_log(a, b):
        d = history[[f"v_{a}", f"v_{b}"]].dropna()
        d = d[(d[f"v_{a}"] > 0) & (d[f"v_{b}"] > 0)]
        return float(np.log(d[f"v_{a}"] / d[f"v_{b}"]).median()) if len(d) >= BIAS_MIN_SECONDS else None
    m_15_5 = median_log("15m", "5m")
    m_15_1h = median_log("15m", "1h")
    if m_15_5 is not None:
        offsets["15m"] = m_15_5
        if m_15_1h is not None:
            offsets["1h"] = m_15_5 - m_15_1h
    return offsets


def redecide(signals, cfg):
    """A copy of the signal table with kind/side/edge/... re-decided under one variant's rules."""
    out = signals.copy()
    for col in votes.SIGNAL_COLUMNS:
        out[col] = None if col not in ("edge", "model") else np.nan
    out["kind"] = "none"
    names_all = [n for n in ("5m", "15m", "1h") if f"bid_{n}" in signals]
    quoted = sum(signals[f"bid_{n}"].notna() & signals[f"ask_{n}"].notna() for n in names_all)
    rows = signals.index[(quoted >= 2) & (signals.tau > 0)]
    history_cols = [f"v_{n}" for n in names_all]
    starts = signals.groupby("expiry").ts.min()
    results = {}
    for expiry, idx in signals.loc[rows].groupby("expiry").groups.items():
        offsets = {}
        if cfg["bias"]:
            earlier = signals[(signals.expiry <= starts[expiry]) & (signals.tau > 60)]
            offsets = bias_offsets(earlier[history_cols])
        for i in idx:
            r = signals.loc[i]
            names = [n for n in names_all if not (pd.isna(r[f"bid_{n}"]) or pd.isna(r[f"ask_{n}"]))]
            nu = r["nu"]
            s = votes.signal(legs_for_row(r, names, cfg["staleness"]), r["sd"], nu, z_limit=cfg["z_limit"],
                             adjust=bias_adjust(offsets) if offsets else None,
                             pair_sigma=shared_pair_sigma(nu) if cfg["staleness"] == "shared" else None,
                             min_votes=cfg["min_votes"])
            results[i] = (s["kind"], s["outlier"], s["lo"], s["hi"], s["side"], s["edge"], s["model"], s["n_votes"])
    if results:
        cols = ["kind", "outlier", "lo", "hi", "side", "edge", "model", "n_votes_variant"]
        decided = pd.DataFrame.from_dict(results, orient="index", columns=cols)
        for col in cols:
            out.loc[decided.index, col] = decided[col]
    for col in ("kind", "outlier", "lo", "hi", "side"):
        out[col] = out[col].astype("string")
    out["kind"] = out["kind"].fillna("none")
    return out


# ============================================================================ evaluating a variant
def exit_pnl(table, trades, after_s):
    """P&L of selling each trade back at bid/ask `after_s` seconds after entry (no resolution needed)."""
    quotes = table.set_index(["expiry", "ts"])
    pnl = []
    for t in trades.itertuples():
        a, b = (t.expiry, t.ts + backtest.DELAY_S * 1000), (t.expiry, t.ts + (backtest.DELAY_S + after_s) * 1000)
        if a not in quotes.index or b not in quotes.index:
            pnl.append(np.nan)
            continue
        qa, qb = quotes.loc[a], quotes.loc[b]
        if t.side == "buy":
            pnl.append((qb[f"bid_{t.lo}"] - qb[f"ask_{t.hi}"]) - (qa[f"ask_{t.lo}"] - qa[f"bid_{t.hi}"]))
        else:
            pnl.append((qa[f"bid_{t.lo}"] - qa[f"ask_{t.hi}"]) - (qb[f"ask_{t.lo}"] - qb[f"bid_{t.hi}"]))
    return trades.assign(pnl=pnl)


def evaluate(table, outcomes, cfg):
    """Headline numbers for one variant on one period."""
    voted = table[table.n_votes >= 2]
    lasting = table[backtest.run_length(table, table.side.notna()) == backtest.PERSIST_S]
    moves = backtest.price_moves(table, lasting, MOVE_HORIZON_S)
    trades, _ = backtest.simulate(table, outcomes, backtest.DEFAULT_THRESHOLD)
    if cfg["exit_after_s"]:
        trades = exit_pnl(table, trades, cfg["exit_after_s"])
    s = backtest.summarise(trades)
    n_moves = len(moves)
    return {
        "events": table.expiry.nunique(),
        "seconds_2plus_votes": len(voted),
        "signal_rate": round((voted.kind != "none").mean(), 4) if len(voted) else np.nan,
        "lasting_signals": len(lasting),
        "lasting_events": lasting.expiry.nunique(),
        "move30_c": round(moves.mid.mean() * 100, 2) if n_moves > 2 else np.nan,
        "move30_se_c": round(moves.mid.std(ddof=1) / math.sqrt(n_moves) * 100, 2) if n_moves > 2 else np.nan,
        "edge_seen_c": round(moves.edge.mean() * 100, 2) if n_moves > 2 else np.nan,
        "trades": len(trades),
        "trades_resolved": s["trades"],
        "hit_rate": round(s["hit_rate"], 3) if s["trades"] else np.nan,
        "pnl_c_per_trade": round(s["pnl_cents"], 2) if s["trades"] else np.nan,
        "pnl_ci_low_c": round(s["ci_low"], 2) if s["trades"] > 1 else np.nan,
        "pnl_ci_high_c": round(s["ci_high"], 2) if s["trades"] > 1 else np.nan,
        "trades_detail": "; ".join(f"{fmt_ts(t.expiry)[5:16]} {t.kind} {t.lo}/{t.hi} {t.side} {t.pnl * 100:+.0f}c"
                                   if pd.notna(t.pnl) else f"{fmt_ts(t.expiry)[5:16]} {t.kind} {t.lo}/{t.hi} {t.side} open"
                                   for t in trades.itertuples()),
    }


# ============================================================================ recording
def git_version():
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=HERE,
                               capture_output=True, text=True).stdout.strip()
        return head + ("+uncommitted" if dirty else "")
    except OSError:
        return "unknown"


def write_summary(latest, run_utc, version):
    lines = ["# Strategy variants", "",
             "Generated by `python variants.py`. Every run is also appended to `variant_runs.csv` with the git",
             "version that produced it, so earlier results stay on record.", "",
             f"Latest run: {run_utc} UTC, code version `{version}`. Rules frozen at {FREEZE_UTC} UTC.", "",
             "Nothing here is tuned on P&L: every variant uses the fixed 4.5-cent edge threshold, the 3-second",
             "persistence rule and a fill one second after the signal.", "",
             "## Variants", ""]
    for name, spec in VARIANTS.items():
        lines.append(f"- **{name}**: {spec['desc']}")
    for period in ("post-freeze", "all"):
        part = latest[latest.period == period]
        if part.empty:
            continue
        first = part.iloc[0]
        lines += ["", f"## {'After the freeze' if period == 'post-freeze' else 'Everything recorded'} "
                      f"({first.data_from} to {first.data_to} UTC, {int(first.events)} events)", "",
                  "| Variant | Signal rate | Lasting signals (events) | Price move after 30 s | Edge seen | Trades | P&L per trade (95% interval) |",
                  "|---|---|---|---|---|---|---|"]
        for r in part.itertuples():
            move = f"{r.move30_c:+.1f} c (se {r.move30_se_c:.1f})" if pd.notna(r.move30_c) else "too few"
            edge = f"{r.edge_seen_c:.1f} c" if pd.notna(r.edge_seen_c) else ""
            if pd.notna(r.pnl_c_per_trade):
                ci = f" ({r.pnl_ci_low_c:+.1f} to {r.pnl_ci_high_c:+.1f})" if pd.notna(r.pnl_ci_low_c) else ""
                pnl = f"{r.pnl_c_per_trade:+.1f} c{ci}"
            else:
                pnl = ""
            trades = f"{int(r.trades)}" + (f", {int(r.trades_resolved)} resolved" if r.trades_resolved != r.trades else "")
            lines.append(f"| {r.variant} | {r.signal_rate:.1%} | {int(r.lasting_signals)} ({int(r.lasting_events)}) "
                         f"| {move} | {edge} | {trades} | {pnl} |")
    lines += ["", "Price move: how far the range's mid moved toward the model 30 s after a signal that held 3 s.",
              "A real mispricing shows a positive move of roughly the edge seen. P&L is held to expiry,",
              "except `exit_30s`, which sells back after 30 s.", ""]
    with open(SUMMARY_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============================================================================ main
def main():
    chosen = [a for a in sys.argv[1:] if not a.startswith("--")] or list(VARIANTS)
    unknown = [c for c in chosen if c not in VARIANTS]
    if unknown:
        sys.exit(f"Unknown variant(s): {', '.join(unknown)}. Known: {', '.join(VARIANTS)}")
    start = int(load("health", 0, now_ms(), columns=["recv_ts"]).recv_ts.min())
    end = now_ms() - 60_000
    freeze = int(pd.Timestamp(FREEZE_UTC, tz="UTC").timestamp() * 1000)
    signals, _ = backtest.load_days(start, end)
    signals = signals.sort_values(["expiry", "ts"]).reset_index(drop=True)
    resolutions = load("resolutions", start, now_ms()).drop_duplicates("slug", keep="last")
    resolutions = resolutions[resolutions.outcome.isin(["Up", "Down"])]
    outcomes = dict(zip(zip(resolutions.close_ts, resolutions.length), (resolutions.outcome == "Up").astype(float)))

    run_utc, version = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M"), git_version()
    print(f"Variant run {run_utc} UTC, code {version}, {signals.expiry.nunique()} events")
    rows = []
    for name in chosen:
        spec = VARIANTS[name]
        cfg = {**DEFAULTS, **{k: v for k, v in spec.items() if k != "desc"}}
        table = redecide(signals, cfg)
        if name == "baseline":                       # the re-decision must reproduce the frozen rules
            same = (table.kind.fillna("none") == signals.kind.fillna("none")) & \
                   (table.side.fillna("") == signals.side.fillna(""))
            print(f"  baseline reproduces the cached decisions on {same.mean():.3%} of seconds")
        for period, part in (("all", table), ("post-freeze", table[table.expiry >= freeze])):
            if part.empty:
                continue
            m = evaluate(part, outcomes, cfg)
            row = {"run_utc": run_utc, "code_version": version, "variant": name, "period": period,
                   "data_from": fmt_ts(part.ts.min())[:16], "data_to": fmt_ts(part.expiry.max())[:16],
                   "params": json.dumps({k: v for k, v in cfg.items()}), **m}
            rows.append(row)
            print(f"  {name:<15} {period:<12} signals {m['signal_rate']:.1%}  lasting {m['lasting_signals']:>3}  "
                  f"move {m['move30_c']} (se {m['move30_se_c']})  trades {m['trades']}  pnl {m['pnl_c_per_trade']}")
    run = pd.DataFrame(rows)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    run.to_csv(RUNS_CSV, mode="a", header=not os.path.exists(RUNS_CSV), index=False)
    write_summary(run, run_utc, version)
    print(f"\nAppended {len(run)} rows to {RUNS_CSV}\nWrote {SUMMARY_MD}")


if __name__ == "__main__":
    main()
