#!/usr/bin/env python3
"""
Parse Sulker's recaps -> realized % AND 'contract high' (max favorable excursion, MFE).
The realized-vs-MFE gap shows how much he leaves on the table = exit-discipline insight.
Run: .venv/bin/python sulker_recaps.py    (or python3)
"""
import sqlite3, re, statistics as st
from pathlib import Path

DB = str(Path(__file__).parent / "channels" / "sulker" / "sulker.db")
line_re = re.compile(r'\b([A-Z]{2,4})?\s*(\d{2,5})\s*([cp])\b', re.I)
mfe_re  = re.compile(r'high[^(]*\(\s*(\d{2,5})\s*%')
KNOWN = {'SPY', 'QQQ', 'IWM', 'SPX', 'NDX', 'DIA', 'TSLA', 'NVDA', 'AAPL', 'META', 'AMZN', 'MSFT'}
sold_re = re.compile(r'(?:sold|exit|runner exit)[^%]*\((-?\d+)\s*%')
pct_re  = re.compile(r'(-?\d+)\s*%')


def parse_line(line):
    tm = line_re.search(line)
    if not tm:
        return None
    strike = int(tm.group(2))
    if not (50 <= strike <= 9000):
        return None
    low = line.lower()
    # MFE (contract high)
    mm = mfe_re.search(low)
    mfe = float(mm.group(1)) if mm else None
    # realized %
    if 'break even' in low or 'b/e' in low:
        real = 0.0
    elif ('zero hero' in low and ('fail' in low or '-100' in low)) or 'fail zero' in low or 'fail target' in low:
        real = -100.0
    else:
        sm = sold_re.search(low)
        if sm:
            real = float(sm.group(1))
        else:
            cleaned = re.split(r'contract high|high\s*-*>', low)[0]  # drop the MFE clause
            pm = pct_re.search(cleaned)
            real = float(pm.group(1)) if pm else None
    if real is None:
        return None
    real = max(min(real, 2000), -100)
    lotto = any(w in low for w in ['lotto', 'zero hero', 'hero to zero', 'starter', 'not full size'])
    tk = (tm.group(1) or '').upper()
    if tk not in KNOWN:
        tk = 'SPX' if strike >= 3000 else 'SPY'      # bare/garbled ticker -> infer from strike size
    return dict(ticker=tk, strike=strike, type=tm.group(3).upper(),
                real=real, mfe=mfe, lotto=lotto)


def main():
    c = sqlite3.connect(DB)
    rows = [(ts, t) for ts, t in c.execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp")]
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:70]
        if k in seen: continue
        seen.add(k); U.append((ts, t))
    recaps = [(ts, t) for ts, t in U if re.match(r'\s*recap', t.lower()) or 'recap:' in t.lower()[:40]]

    trades, key = [], set()
    for ts, t in recaps:
        for line in re.split(r'\n', t):
            r = parse_line(line)
            if not r: continue
            k = (ts[:10], r['ticker'], r['strike'], r['type'], r['real'])
            if k in key: continue
            key.add(k); r['date'] = ts[:10]; trades.append(r)

    rr = [t['real'] for t in trades]
    wins = [x for x in rr if x > 1]
    losses = [x for x in rr if x < -1]
    gp, gl = sum(wins), -sum(losses)
    print(f"=========  SULKER — {len(trades)} documented trades from {len(recaps)} recaps  =========\n")
    print(f"  Win rate     : {len(wins)/len(rr)*100:.1f}%  ({len(wins)}W / {len(losses)}L / {len(rr)-len(wins)-len(losses)} BE)")
    print(f"  Avg return   : {st.mean(rr):+.1f}%/trade   median {st.median(rr):+.1f}%")
    print(f"  Avg winner   : {st.mean(wins):+.1f}%    avg loser {st.mean(losses):+.1f}%")
    print(f"  Profit factor: {gp/gl:.2f}")
    print(f"  Best / worst : {max(rr):+.0f}% / {min(rr):+.0f}%")

    # MFE / capture analysis — the exit-discipline gold
    paired = [t for t in trades if t['mfe'] and t['real'] > -99]
    if paired:
        caps = [t['real']/t['mfe'] for t in paired if t['mfe'] > 5]
        print(f"\n  --- EXIT DISCIPLINE (realized vs 'contract high' MFE, n={len(paired)}) ---")
        print(f"  Median MFE (how far trades ran): +{st.median([t['mfe'] for t in paired]):.0f}%")
        print(f"  Median realized:                 +{st.median([t['real'] for t in paired]):.0f}%")
        print(f"  Median capture ratio: {st.median(caps)*100:.0f}%  (he banks ~this share of the peak move)")
        left = [t['mfe']-t['real'] for t in paired]
        print(f"  Median left on the table: {st.median(left):.0f} pts of premium move")

    # lotto vs normal
    for label, sub in [("Normal", [t for t in trades if not t['lotto']]),
                       ("Lotto/hero", [t for t in trades if t['lotto']])]:
        s = [t['real'] for t in sub]
        if s:
            w = sum(1 for x in s if x > 1)
            print(f"    {label:<11} n={len(s):>4}  win {w/len(s)*100:.0f}%  median {st.median(s):+.0f}%  mean {st.mean(s):+.0f}%")

    print("\n  sample parsed:")
    for t in trades[:8]:
        print(f"    {t['date']} {t['ticker']} {t['strike']}{t['type']}  real {t['real']:+.0f}%  "
              f"MFE {('+'+str(int(t['mfe']))+'%') if t['mfe'] else '—'}{'  [lotto]' if t['lotto'] else ''}")


if __name__ == "__main__":
    main()
