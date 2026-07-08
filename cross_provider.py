#!/usr/bin/env python3
"""
Cross-provider DEEP analysis — positive, actionable outputs across all channels.
  1. Provider Edge Scorecard (trust + executable edge).
  2. Where the edge CONCENTRATES (pooled Mike+Sulker segment table) — the positive part.
  3. Sulker exit-target curve from 'contract high' (MFE): P(reach +T%) + optimal take-profit.
  4. Best day-of-week.
Run: .venv/bin/python cross_provider.py   (or python3)
"""
import sqlite3, re, statistics as st
from pathlib import Path
from datetime import date
ROOT = Path(__file__).parent

PROV = ['jmt', 'mike', 'sulker', 'prince', 'prince_sac', 'optionking']
line_re = re.compile(r'\b([A-Z]{2,4})?\s*(\d{2,5})\s*([cp])\b', re.I)
mfe_re  = re.compile(r'high[^%]{0,18}\(?\s*(\d{2,5})\s*%')
pct_re  = re.compile(r'(-?\d+)\s*%')
prem_re = re.compile(r'(\d{0,2}\.\d{1,2})')


def load_dedup(ch):
    db = ROOT / "channels" / ch / f"{ch}.db"
    if not db.exists():
        return []
    rows = sqlite3.connect(str(db)).execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp").fetchall()
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:70]
        if k in seen: continue
        seen.add(k); U.append((ts, t))
    return U


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


def parse_recaps(ch):
    out = []
    for ts, t in load_dedup(ch):
        if 'recap' not in t.lower()[:40]:
            continue
        for line in t.split('\n'):
            m = line_re.search(line)
            if not m: continue
            r = realized(line)
            if r is None: continue
            mm = mfe_re.search(line.lower())
            pm = prem_re.search(re.split(r'contract high', line.lower())[0])
            out.append(dict(date=ts[:10], provider=ch, ticker=(m.group(1) or 'SPX').upper(),
                            type=m.group(3).upper(), premium=float(pm.group(1)) if pm else None,
                            lotto=int(any(w in line.lower() for w in ['lotto', 'hero', 'starter'])),
                            eod=int('eod' in line.lower()), ret=max(min(r, 2000), -100),
                            mfe=float(mm.group(1)) if mm else None))
    return out


def bias_counts(ch):
    U = load_dedup(ch)
    low = [t.lower() for _, t in U]
    win = sum(1 for t in low if re.search(r'up \d+%', t) or "i'm out" in t or 'profit' in t or 'banger' in t)
    loss = sum(1 for t in low if any(w in t for w in ['stopped', 'cut out', 'took a loss', ' -100', 'expired worthless']))
    ent = sum(1 for t in low if re.search(r'\b\d{2,5}\s*[cp]\b', t[:35]) and ('@' in t[:40] or 'entry' in t or 's/l' in t or ' at ' in t))
    return len(U), ent, win, loss


def stats(rr):
    w = [x for x in rr if x > 1]; l = [x for x in rr if x < -1]
    return dict(n=len(rr), win=len(w)/len(rr)*100, med=st.median(rr), mean=st.mean(rr),
                pf=(sum(w)/-sum(l) if l else 99), avgw=st.mean(w) if w else 0, avgl=st.mean(l) if l else 0)


# ---------- 1. SCORECARD ----------
print("=" * 74)
print("1. PROVIDER EDGE SCORECARD")
print("=" * 74)
print(f"{'provider':<12}{'msgs':>7}{'entries':>8}{'win:loss msg':>14}{'documented?':>13}")
docs = {}
for ch in PROV:
    n, ent, win, loss = bias_counts(ch)
    tr = parse_recaps(ch)
    docs[ch] = tr
    ratio = f"{win}:{loss}"
    tag = f"YES ({len(tr)} trades)" if len(tr) > 40 else "no (highlight reel)"
    print(f"{ch:<12}{n:>7}{ent:>8}{ratio:>14}{tag:>22}")

# ---------- 2. WHERE EDGE CONCENTRATES (pooled documented) ----------
pool = docs['mike'] + docs['sulker']
print("\n" + "=" * 74)
print(f"2. WHERE THE EDGE CONCENTRATES  (pooled Mike+Sulker, {len(pool)} documented trades)")
print("=" * 74)
def seg(label, sub):
    rr = [t['ret'] for t in sub]
    if len(rr) < 15: return
    s = stats(rr)
    print(f"  {label:<20} n={s['n']:>4}  win {s['win']:>3.0f}%  median {s['med']:>+4.0f}%  PF {s['pf']:>4.1f}")
seg("ALL", pool)
print("  -- by ticker --")
for tk in ['SPX', 'SPY', 'QQQ', 'IWM', 'NVDA', 'TSLA', 'MSFT']:
    seg(tk, [t for t in pool if t['ticker'] == tk])
print("  -- by premium --")
seg("cheap <=$0.50", [t for t in pool if t['premium'] and t['premium'] <= 0.5])
seg("mid $0.50-1.50", [t for t in pool if t['premium'] and 0.5 < t['premium'] <= 1.5])
seg("rich >$1.50", [t for t in pool if t['premium'] and t['premium'] > 1.5])
print("  -- by type / style --")
seg("Calls", [t for t in pool if t['type'] == 'C'])
seg("Puts", [t for t in pool if t['type'] == 'P'])
seg("Lotto", [t for t in pool if t['lotto']])
seg("Non-lotto", [t for t in pool if not t['lotto']])

# ---------- 3. SULKER EXIT-TARGET CURVE (from MFE) ----------
smfe = [t for t in docs['sulker'] if t['mfe'] and t['mfe'] > 0]
print("\n" + "=" * 74)
print(f"3. EXIT OPTIMIZATION — how far Sulker's trades run (MFE, n={len(smfe)})")
print("=" * 74)
if smfe:
    mfes = [t['mfe'] for t in smfe]
    print("  P(a trade reaches at least +T%):")
    for T in [20, 30, 50, 75, 100, 150, 200, 300]:
        p = sum(1 for m in mfes if m >= T) / len(mfes) * 100
        print(f"    +{T:>4}% :  {p:>3.0f}%   {'#'*int(p/3)}")
    print("\n  Fixed take-profit expectancy (sell all at +T if it ever reached +T, else his exit):")
    print(f"    {'target':>8}{'hit%':>7}{'exp/trade':>12}")
    for T in [20, 30, 40, 50, 75, 100]:
        rr = [T if t['mfe'] >= T else t['ret'] for t in smfe]
        hit = sum(1 for t in smfe if t['mfe'] >= T) / len(smfe) * 100
        print(f"    +{T:>4}%{hit:>7.0f}%{st.mean(rr):>+11.0f}%")
    print("  -> the peak of exp/trade is roughly your best mechanical take-profit.")

# ---------- 4. BEST DAY OF WEEK ----------
print("\n" + "=" * 74)
print("4. BEST DAY OF WEEK (pooled documented)")
print("=" * 74)
from collections import defaultdict
byd = defaultdict(list)
for t in pool:
    try: byd[date.fromisoformat(t['date']).strftime('%a')].append(t['ret'])
    except Exception: pass
for d in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']:
    rr = byd.get(d, [])
    if rr:
        s = stats(rr)
        print(f"  {d}  n={s['n']:>4}  win {s['win']:>3.0f}%  median {s['med']:>+4.0f}%")
