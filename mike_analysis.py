#!/usr/bin/env python3
"""
Complete analysis of SPX Mike's recap trades -> inputs for an execution plan.
Sections: segment edges, trades/day, drawdown & losing streaks, Kelly sizing via
Monte Carlo, and slippage sensitivity (the real follower-killer).

Run:  .venv/bin/python mike_analysis.py
"""
import sqlite3, re, math, statistics as st
from pathlib import Path
from collections import Counter, defaultdict
import numpy as np

DB = str(Path(__file__).parent / "channels" / "mike" / "mike.db")
rng = np.random.default_rng(7)

pct_re = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*%')
trade_re = re.compile(r'\b([A-Z]{2,5})?\s*(\d{2,4}(?:\.\d)?)\s*([CP])\b')
arrow_re = re.compile(r'(\d*\.?\d+)\s*(?:→|->)\s*([\d.]+)')


def parse_line(line, session):
    tm = trade_re.search(line)
    if not tm:
        return None
    low = line.lower()
    pm = pct_re.search(line)
    if pm:                                                pct = float(pm.group(1))
    elif 'expired worthless' in low or '-100' in low:     pct = -100.0
    elif any(w in low for w in ['scratch', 'flat', '~be', 'breakeven']): pct = 0.0
    elif 'stopped' in low:                                pct = -50.0
    elif 'cut' in low or 'loss' in low:                   pct = -40.0
    else:                                                 return None
    pct = max(pct, -100.0)                                # long options can't lose >100%
    am = arrow_re.search(line)
    entry = float(am.group(1)) if (am and am.group(1) not in ('', '.')) else None
    # count documented trims as a measure of how he scales out
    trims = line.count('/') + line.count('→') - 1
    return dict(ticker=(tm.group(1) or 'SPX').upper(), type=tm.group(3).upper(), pct=pct,
                entry=entry, lotto=int('lotto' in low), eod=int('eod' in low or session == 'EOD'),
                session=session, trims=max(trims, 0))


def load():
    c = sqlite3.connect(DB)
    rows = [(ts, t) for ts, t in c.execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp")]
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:80]
        if k in seen: continue
        seen.add(k); U.append((ts, t))
    recaps = [(ts, t) for ts, t in U if 'recap' in t.lower() and '├─' not in t]
    trades, key = [], set()
    for ts, t in recaps:
        date = ts[:10]; session = 'UNK'
        for line in t.split('\n'):
            ll = line.lower()
            if 'am session' in ll: session = 'AM'
            elif 'pm session' in ll: session = 'PM'
            elif 'eod' in ll and ('lotto' in ll or 'session' in ll): session = 'EOD'
            elif 'regular market' in ll: session = 'REG'
            r = parse_line(line, session)
            if not r: continue
            k = (r['ticker'], r['type'], r['entry'], r['pct'], date)
            if k in key: continue
            key.add(k); r['date'] = date; trades.append(r)
    return trades


def seg(name, rets):
    if len(rets) < 8: return
    a = np.array(rets)
    print(f"  {name:<20} n={len(a):>4}  win {(a>0.001).mean()*100:>3.0f}%  "
          f"mean {a.mean()*100:>+6.0f}%  median {np.median(a)*100:>+5.0f}%")


def main():
    T = load()
    R = np.array([t['pct'] / 100 for t in T])
    n = len(T)
    print(f"==================  COMPLETE ANALYSIS — {n} trades  ==================\n")

    print("OVERALL")
    seg("ALL", R)
    print(f"  distribution: p10 {np.percentile(R,10)*100:+.0f}%  p25 {np.percentile(R,25)*100:+.0f}%  "
          f"p50 {np.percentile(R,50)*100:+.0f}%  p75 {np.percentile(R,75)*100:+.0f}%  p90 {np.percentile(R,90)*100:+.0f}%")
    wins = R[R > 0.001]; losses = R[R < -0.001]
    print(f"  win {len(wins)/n*100:.0f}%  avgW {wins.mean()*100:+.0f}%  avgL {losses.mean()*100:+.0f}%  "
          f"PF {wins.sum()/-losses.sum():.1f}  expectancy {R.mean()*100:+.0f}%/trade")

    print("\nBY SESSION")
    for s in ['AM', 'PM', 'REG', 'EOD', 'UNK']:
        seg(s, [t['pct']/100 for t in T if t['session'] == s])
    print("\nBY TICKER (top 8 by volume)")
    for tk, _ in Counter(t['ticker'] for t in T).most_common(8):
        seg(tk, [t['pct']/100 for t in T if t['ticker'] == tk])
    print("\nBY ENTRY PREMIUM")
    seg("<= $0.50 (cheap)", [t['pct']/100 for t in T if t['entry'] and t['entry'] <= 0.5])
    seg("$0.50-1.50", [t['pct']/100 for t in T if t['entry'] and 0.5 < t['entry'] <= 1.5])
    seg("> $1.50", [t['pct']/100 for t in T if t['entry'] and t['entry'] > 1.5])

    # trades per day
    perday = Counter(t['date'] for t in T)
    cnts = sorted(perday.values())
    print(f"\nWORKLOAD: {len(perday)} active days, "
          f"median {cnts[len(cnts)//2]} trades/day, max {max(cnts)}/day "
          f"(you can't take them all — selection doesn't help, so cap your own count).")

    # ---- drawdown & losing streaks (equal-size bets, fraction f of bankroll) ----
    print("\n================  RISK: DRAWDOWN & LOSING STREAKS  ================")
    streak = mx = 0
    for r in R:
        streak = streak + 1 if r < -0.001 else 0
        mx = max(mx, streak)
    print(f"  Longest losing streak in history: {mx} trades in a row.")

    def sim_curve(frac, seq):
        eq = 1.0; peak = 1.0; dd = 0.0
        for r in seq:
            eq *= (1 + frac * max(r, -1))
            peak = max(peak, eq); dd = max(dd, (peak - eq) / peak)
        return eq, dd

    # ---- empirical Kelly (maximize E[log(1+f r)]) ----
    def growth(frac):
        return np.mean(np.log1p(frac * np.clip(R, -0.999, None)))
    fs = np.linspace(0.01, 1.0, 100)
    kelly = fs[np.argmax([growth(f) for f in fs])]
    print(f"\n================  POSITION SIZING (KELLY + MONTE CARLO)  ================")
    print(f"  Full-Kelly fraction (on self-reported returns): {kelly*100:.0f}% of bankroll/trade")
    print(f"  -> Full Kelly is far too aggressive on fat-tailed, optimistic data.")

    print(f"\n  Monte Carlo: 3000 resampled 200-trade sequences at each bet size:")
    print(f"  {'bet/trade':>10}{'median x':>10}{'5th-pct x':>11}{'med maxDD':>11}{'P(loss)':>9}")
    for frac in [0.02, 0.05, 0.10, 0.15, 0.25]:
        finals, dds = [], []
        for _ in range(3000):
            seq = R[rng.integers(0, n, 200)]
            eq, dd = sim_curve(frac, seq)
            finals.append(eq); dds.append(dd)
        finals = np.array(finals); dds = np.array(dds)
        print(f"  {frac*100:>8.0f}% {np.median(finals):>9.1f}x{np.percentile(finals,5):>10.1f}x"
              f"{np.median(dds)*100:>9.0f}% {(finals<1).mean()*100:>8.0f}%")
    print("  (median x = typical 200-trade growth; 5th-pct = unlucky case; maxDD = drawdown pain)")

    # ---- slippage sensitivity (the real follower edge) ----
    print(f"\n================  SLIPPAGE SENSITIVITY (your REAL edge)  ================")
    print("  Model: you buy worse and sell worse than Mike's quoted fills by s% each side.")
    print(f"  {'slippage':>10}{'exp/trade':>12}{'win%':>8}{'PF':>7}")
    ent = np.array([t['entry'] if t['entry'] else 1.0 for t in T])
    for s in [0.0, 0.05, 0.10, 0.15, 0.20]:
        # cheap options get ~2x slippage (wider spreads)
        s_eff = np.where(ent <= 0.5, s * 2, s)
        gross = 1 + R
        real = gross * (1 - s_eff) / (1 + s_eff) - 1
        real = np.clip(real, -1, None)
        w = real[real > 0.001]; l = real[real < -0.001]
        pf = w.sum() / -l.sum() if len(l) else float('inf')
        print(f"  {s*100:>8.0f}% {real.mean()*100:>+10.0f}%{(real>0.001).mean()*100:>7.0f}%{pf:>7.1f}")
    print("  (cheap <=$0.50 options modeled at 2x slippage — that's where edge evaporates)")


if __name__ == "__main__":
    main()
