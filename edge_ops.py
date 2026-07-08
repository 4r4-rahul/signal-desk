#!/usr/bin/env python3
"""
Answers the SME's highest-value, data-answerable questions for a $15k follower:
  Q4  Intraday correlation & TRUE daily drawdown (the red-day risk).
  Q14 Outlier concentration (does a few trades carry the P&L? -> trail vs fixed target).
  Q5  Fat-tail Kelly sizing per provider.
  Q6  Mike<->Sulker correlation & the diversification benefit.
  Q8  Edge decay (recent vs historical).
Run: .venv/bin/python edge_ops.py
"""
import sqlite3, re, math, statistics as st
from pathlib import Path
from collections import defaultdict
from datetime import date
ROOT = Path(__file__).parent
line_re = re.compile(r'\b([A-Z]{2,4})?\s*(\d{2,5})\s*([cp])\b', re.I)
pct_re = re.compile(r'(-?\d+)\s*%')


def realized(line):
    low = line.lower()
    if 'break even' in low or 'b/e' in low: return 0.0
    if 'expired worthless' in low or ('zero hero' in low and ('fail' in low or '-100' in low)): return -100.0
    if 'fail zero' in low or 'fail target' in low: return -100.0
    sm = re.search(r'(?:sold|exit)[^%]*\((-?\d+)\s*%', low)
    if sm: return float(sm.group(1))
    cleaned = re.split(r'contract high|high\s*-*>', low)[0]
    pm = pct_re.search(cleaned)
    if pm: return float(pm.group(1))
    if 'scratch' in low or 'flat' in low: return 0.0
    if 'stopped' in low: return -50.0
    if 'cut' in low or 'loss' in low: return -40.0
    return None


def parse(ch):
    db = ROOT / "channels" / ch / f"{ch}.db"
    rows = sqlite3.connect(str(db)).execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp").fetchall()
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:70]
        if k in seen: continue
        seen.add(k); U.append((ts, t))
    out = []
    for ts, t in U:
        if 'recap' not in t.lower()[:40]: continue
        for line in t.split('\n'):
            if not line_re.search(line): continue
            r = realized(line)
            if r is None: continue
            out.append((ts[:10], max(min(r, 2000), -100) / 100.0))
    return out


def pct(xs, p):
    xs = sorted(xs); i = int(p * (len(xs) - 1)); return xs[i]


mike, sulk = parse('mike'), parse('sulker')
pool = mike + sulk
allr = [r for _, r in pool]

# ---- Q14 outlier concentration ----
print("=" * 70)
print("Q14. IS THE EDGE OUTLIER-DRIVEN?  (decides trail-vs-fixed-target)")
print("=" * 70)
srt = sorted(allr, reverse=True)
gross_pos = sum(x for x in srt if x > 0)
topN = lambda f: sum(srt[:max(1, int(len(srt) * f))]) / gross_pos * 100
print(f"  Top  5% of trades = {topN(.05):.0f}% of all gross profit")
print(f"  Top 10% of trades = {topN(.10):.0f}% of all gross profit")
print(f"  Top 20% of trades = {topN(.20):.0f}% of all gross profit")
net = sum(allr)
print(f"  Net sum of all trade returns: {net:.0f}R  |  remove top 5% of trades -> {sum(srt[int(len(srt)*.05):]):.0f}R")
print("  -> If a few trades carry it, DON'T cap winners with a tight target; TRAIL instead.")

# ---- Q4 daily drawdown / clustering ----
print("\n" + "=" * 70)
print("Q4. TRUE DAILY RISK (per-trade win% hides correlated red days)")
print("=" * 70)
for name, tr in [('Mike', mike), ('Sulker', sulk), ('Pooled', pool)]:
    daily = defaultdict(list)
    for d, r in tr: daily[d].append(r)
    day_avg = [st.mean(v) for v in daily.values()]          # equal-weight daily return
    k = st.mean([len(v) for v in daily.values()])
    sig_t = st.pstdev([r for _, r in tr])
    indep = sig_t / math.sqrt(k)                            # daily std if trades were independent
    print(f"  {name:<7} days={len(day_avg):>3}  avg trades/day={k:.1f}  "
          f"median day {st.median(day_avg)*100:+.0f}%  worst day {min(day_avg)*100:+.0f}%  "
          f"P5 day {pct(day_avg,.05)*100:+.0f}%")
    print(f"          daily std {st.pstdev(day_avg)*100:.0f}%  vs {indep*100:.0f}% if independent "
          f"-> clustering x{st.pstdev(day_avg)/indep:.1f}")

# ---- Q6 correlation & diversification ----
print("\n" + "=" * 70)
print("Q6. DIVERSIFICATION: do Mike & Sulker move together?")
print("=" * 70)
def daily_map(tr):
    dd = defaultdict(list)
    for d, r in tr: dd[d].append(r)
    return {d: st.mean(v) for d, v in dd.items()}
mm, ss = daily_map(mike), daily_map(sulk)
common = sorted(set(mm) & set(ss))
if len(common) > 10:
    a = [mm[d] for d in common]; b = [ss[d] for d in common]
    ma, mb = st.mean(a), st.mean(b)
    cov = sum((x-ma)*(y-mb) for x, y in zip(a, b)) / len(a)
    corr = cov / (st.pstdev(a)*st.pstdev(b))
    blend = [(x+y)/2 for x, y in zip(a, b)]
    print(f"  Overlapping days: {len(common)}   correlation: {corr:+.2f}")
    print(f"  Daily std — Mike {st.pstdev(a)*100:.0f}%  Sulker {st.pstdev(b)*100:.0f}%  "
          f"50/50 blend {st.pstdev(blend)*100:.0f}%")
    print(f"  -> lower blend std = free drawdown reduction from following BOTH.")

# ---- Q5 fat-tail Kelly ----
print("\n" + "=" * 70)
print("Q5. FAT-TAIL KELLY SIZING (per provider, from realized returns)")
print("=" * 70)
def kelly(rs):
    best_f, best = 0, -9
    f = 0.0
    while f <= 1.0:
        g = sum(math.log(max(1e-9, 1 + f*r)) for r in rs) / len(rs)
        if g > best: best, best_f = g, f
        f += 0.01
    return best_f
for name, tr in [('Mike', mike), ('Sulker', sulk)]:
    rs = [r for _, r in tr]
    f = kelly(rs)
    print(f"  {name:<7} full-Kelly f*={f*100:.0f}% of bankroll/trade  "
          f"-> use QUARTER-Kelly ~{f*25:.0f}% (fat tails + self-report inflation)")
print("  NOTE: returns are self-reported (inflated), so even quarter-Kelly is aggressive.")
print("  Practical: cap at 1-2% risk/trade regardless of what Kelly says.")

# ---- Q8 edge decay ----
print("\n" + "=" * 70)
print("Q8. EDGE DECAY: recent vs historical")
print("=" * 70)
for name, tr in [('Mike', mike), ('Sulker', sulk)]:
    tr = sorted(tr)
    cut = tr[-len(tr)//4:]                                  # last 25% of trades (recent)
    old = tr[:-len(tr)//4]
    wr = lambda s: sum(1 for _, r in s if r > 0.01)/len(s)*100
    md = lambda s: st.median([r for _, r in s])*100
    print(f"  {name:<7} historical win {wr(old):.0f}% / median {md(old):+.0f}%   "
          f"recent win {wr(cut):.0f}% / median {md(cut):+.0f}%   "
          f"{'⚠ DECAY' if wr(cut) < wr(old)-6 else 'stable'}")
