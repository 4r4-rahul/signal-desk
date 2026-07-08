#!/usr/bin/env python3
"""
Lotto vs Normal: how Mike's two bet types differ, to inform YOUR execution & sizing.
Run: .venv/bin/python mike_lotto.py
"""
import sqlite3, re, statistics as st
from pathlib import Path
import numpy as np

DB = str(Path(__file__).parent / "channels" / "mike" / "mike.db")
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
    pct = max(pct, -100.0)
    am = arrow_re.search(line)
    entry = float(am.group(1)) if (am and am.group(1) not in ('', '.')) else None
    return dict(pct=pct, entry=entry, lotto=('lotto' in low), eod=('eod' in low or session == 'EOD'),
                small=('small' in low or 'starter' in low),
                houseprofits=('w/ profits' in low or 'w profits' in low or 'house' in low),
                line=line.strip())


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
        session = 'UNK'
        for line in t.split('\n'):
            ll = line.lower()
            if 'eod' in ll and ('lotto' in ll or 'session' in ll): session = 'EOD'
            r = parse_line(line, session)
            if not r: continue
            k = (r['pct'], r['entry'], r['line'][:30])
            if k in key: continue
            key.add(k); trades.append(r)
    return trades


def profile(name, sub):
    if not sub: return
    r = np.array([t['pct']/100 for t in sub])
    n = len(r)
    zero = (r <= -0.99).mean()          # total loss (zero/hero)
    moon = (r > 2.0).mean()             # +200%+ moonshot
    w = r[r > 0.001]; l = r[r < -0.001]
    print(f"  {name:<16} n={n:>4}  win {(r>0.001).mean()*100:>3.0f}%  median {np.median(r)*100:>+5.0f}%  "
          f"mean {r.mean()*100:>+5.0f}%  -100%:{zero*100:>3.0f}%  moonshot:{moon*100:>3.0f}%  "
          f"avgW {w.mean()*100 if len(w) else 0:>+4.0f}%  avgL {l.mean()*100 if len(l) else 0:>+4.0f}%")


def main():
    T = load()
    lot = [t for t in T if t['lotto']]
    norm = [t for t in T if not t['lotto']]
    print(f"================  LOTTO vs NORMAL  ({len(T)} trades)  ================\n")
    print("                       n    win   median   mean   total-loss  moonshot   avgW   avgL")
    profile("ALL", T)
    profile("NORMAL", norm)
    profile("LOTTO", lot)
    profile("  EOD lotto", [t for t in lot if t['eod']])
    profile("  intraday lotto", [t for t in lot if not t['eod']])

    print(f"\nFREQUENCY: lottos are {len(lot)/len(T)*100:.0f}% of his documented trades.")

    # how he sizes lottos (language signal)
    small = [t for t in lot if t['small']]
    house = [t for t in lot if t['houseprofits']]
    print(f"\nHIS LOTTO SIZING LANGUAGE:")
    print(f"  lottos tagged 'small'/'starter'      : {len(small)}")
    print(f"  lottos tagged 'w/ profits'/'house'   : {len(house)}  (i.e. played with house money)")

    # premium of lottos vs normal
    le = [t['entry'] for t in lot if t['entry']]
    ne = [t['entry'] for t in norm if t['entry']]
    if le and ne:
        print(f"\nPREMIUMS:  lotto median ${st.median(le):.2f}  |  normal median ${st.median(ne):.2f}")
        cheap_lot = sum(1 for e in le if e <= 0.5) / len(le)
        print(f"  {cheap_lot*100:.0f}% of lottos are <= $0.50 (widest spreads / worst real fills).")

    # what a fixed-fraction bettor earns on each bucket AFTER realistic 15% slippage
    def real_exp(sub, slip):
        r = np.array([t['pct']/100 for t in sub]); e = np.array([t['entry'] or 1 for t in sub])
        s = np.where(e <= 0.5, slip*2, slip)
        rr = np.clip((1+r)*(1-s)/(1+s) - 1, -1, None)
        return rr.mean()
    print(f"\nREALISTIC EDGE @15% base slippage (cheap=2x):")
    print(f"  NORMAL exp/trade: {real_exp(norm,0.15)*100:+.0f}%   LOTTO exp/trade: {real_exp(lot,0.15)*100:+.0f}%")

    print("\n  sample lotto lines:")
    for t in [x for x in lot if x['small'] or x['houseprofits']][:6]:
        print("   ", t['line'][:75])


if __name__ == "__main__":
    main()
