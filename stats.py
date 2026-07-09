#!/usr/bin/env python3
"""
Performance analytics from REAL closed trades (channels/*/closed_trades.jsonl).

Computes the metrics that actually tell you if there's an edge — expectancy, win rate,
avg win/loss, profit factor, payoff ratio, max drawdown, Kelly — overall AND per provider.
Everything is on YOUR realized dollars (not provider self-reports), so it's honest.

    python3 stats.py         # print the performance report
"""
import json, glob
from pathlib import Path

ROOT = Path(__file__).parent


def _load():
    out = []
    for f in glob.glob(str(ROOT / "channels" / "*" / "closed_trades.jsonl")):
        try:
            for ln in Path(f).read_text().splitlines():
                if ln.strip():
                    out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _metrics(trades):
    """All the performance stats for a set of closed trades, on realized $."""
    trades = sorted(trades, key=lambda t: t.get("closed_utc") or t.get("closed") or "")
    pnls = [float(t.get("realized") or 0) for t in trades]
    n = len(pnls)
    if not n:
        return {"n": 0, "total": 0.0}
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    nw, nl = len(wins), len(losses)
    total = round(sum(pnls), 2)
    gw, gl = sum(wins), abs(sum(losses))                 # gross win / gross loss
    avg_win = round(gw / nw, 2) if nw else 0.0
    avg_loss = round(gl / nl, 2) if nl else 0.0          # positive number
    win_rate = round(nw / n * 100, 1)
    profit_factor = round(gw / gl, 2) if gl > 0 else None    # None = ∞ (no losses)
    payoff = round(avg_win / avg_loss, 2) if avg_loss > 0 else None
    expectancy = round(total / n, 2)                     # avg $ per trade = the honest expectancy
    rr = [float(t["R_multiple"]) for t in trades if t.get("R_multiple") is not None]
    expectancy_R = round(sum(rr) / len(rr), 2) if rr else None
    # equity curve (cumulative $) + max drawdown
    eq = peak = maxdd = 0.0
    curve = []
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
        curve.append(round(eq, 2))
    # fractional Kelly (theoretical optimal bet) — noisy on small n, shown with a caveat in the UI
    kelly = None
    if payoff and win_rate:
        w = win_rate / 100
        kelly = round((w - (1 - w) / payoff) * 100, 1)
    holds = [t.get("hold_secs") for t in trades if t.get("hold_secs") is not None]
    return {"n": n, "wins": nw, "losses": nl, "win_rate": win_rate, "total": total,
            "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": profit_factor, "payoff": payoff,
            "expectancy": expectancy, "expectancy_R": expectancy_R,
            "best": round(max(pnls), 2), "worst": round(min(pnls), 2), "max_dd": round(maxdd, 2),
            "kelly_pct": kelly, "curve": curve,
            "avg_hold_min": round(sum(holds) / len(holds) / 60, 1) if holds else None}


def _drawdown(trades):
    """Drawdown analytics on the realized equity curve — current DD (below the all-time high), max DD
    and WHEN it bottomed, whether it recovered, and how long since a new high. This is what keeps you
    safe: manage from your CURRENT distance to the peak, not a static all-time number."""
    trades = sorted(trades, key=lambda t: t.get("closed_utc") or t.get("closed") or "")
    if not trades:
        return {}
    eq = run_peak = hwm = 0.0
    run_peak_i = hwm_i = -1
    maxdd = 0.0
    mdd_peak_i = mdd_trough_i = -1

    def _lab(i):
        if i < 0 or i >= len(trades):
            return None
        t = trades[i]
        return {"date": (t.get("closed") or t.get("closed_utc") or "")[:10],
                "leg": f"{t.get('ticker')} {t.get('leg')}", "provider": t.get("provider")}

    for i, t in enumerate(trades):
        eq += float(t.get("realized") or 0)
        if eq >= run_peak:
            run_peak, run_peak_i = eq, i
        dd = eq - run_peak
        if dd < maxdd:
            maxdd, mdd_trough_i, mdd_peak_i = dd, i, run_peak_i
        if eq >= hwm:
            hwm, hwm_i = eq, i
    cur_dd = round(eq - hwm, 2)                        # how far below the all-time high, right now
    trough_eq = round(sum(float(t.get("realized") or 0) for t in trades[:mdd_trough_i + 1]), 2) if mdd_trough_i >= 0 else 0
    return {"hwm": round(hwm, 2), "current_eq": round(eq, 2), "current_dd": cur_dd,
            "underwater": cur_dd < -0.01, "trades_since_hwm": (len(trades) - 1 - hwm_i) if hwm_i >= 0 else 0,
            "max_dd": round(maxdd, 2), "max_dd_peak": _lab(mdd_peak_i), "max_dd_trough": _lab(mdd_trough_i),
            "peak_eq": round(sum(float(t.get("realized") or 0) for t in trades[:mdd_peak_i + 1]), 2) if mdd_peak_i >= 0 else 0,
            "trough_eq": trough_eq, "recovered": eq > trough_eq}


def performance():
    """Overall + per-provider performance. Providers ranked by realized total."""
    trades = _load()
    byp = {}
    for t in trades:
        byp.setdefault(t.get("provider") or "unknown", []).append(t)
    providers = {p: _metrics(ts) for p, ts in byp.items()}
    providers = dict(sorted(providers.items(), key=lambda kv: kv[1].get("total", 0), reverse=True))
    # per-DAY P&L (grouped by the ET trading date the trade closed)
    byday = {}
    for t in trades:
        d = (t.get("closed") or t.get("closed_utc") or "")[:10]     # YYYY-MM-DD (ET close)
        if d:
            byday.setdefault(d, []).append(float(t.get("realized") or 0))
    days = [{"date": d, "total": round(sum(v), 2), "n": len(v),
             "wins": sum(1 for x in v if x > 0), "losses": sum(1 for x in v if x < 0)}
            for d, v in sorted(byday.items())]
    return {"overall": _metrics(trades), "providers": providers, "by_day": days,
            "drawdown": _drawdown(trades)}


if __name__ == "__main__":
    import sys
    r = performance()
    o = r["overall"]
    if not o["n"]:
        print("No closed trades yet — take & close some trades, then the stats populate."); sys.exit()
    print(f"\n{'='*56}\nPERFORMANCE  ({o['n']} closed trades)\n{'='*56}")
    print(f"  Total P&L    : ${o['total']:+,.0f}")
    print(f"  Win rate     : {o['win_rate']}%  ({o['wins']}W / {o['losses']}L)")
    print(f"  Expectancy   : ${o['expectancy']:+.2f}/trade" + (f"  ({o['expectancy_R']:+.2f}R)" if o['expectancy_R'] is not None else ""))
    print(f"  Avg win/loss : +${o['avg_win']:.0f} / -${o['avg_loss']:.0f}   payoff {o['payoff']}")
    print(f"  Profit factor: {o['profit_factor']}   max DD ${o['max_dd']:.0f}   Kelly {o['kelly_pct']}%")
    print(f"\n  {'PROVIDER':<13}{'N':>4}{'WIN%':>7}{'EXPECT':>9}{'PF':>6}{'TOTAL':>9}")
    for p, m in r["providers"].items():
        print(f"  {p:<13}{m['n']:>4}{m['win_rate']:>6}%{('$'+format(m['expectancy'],'+.0f')):>9}"
              f"{str(m['profit_factor']):>6}{('$'+format(m['total'],'+.0f')):>9}")
