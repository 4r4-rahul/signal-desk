#!/usr/bin/env python3
"""
Parse SPX Mike's EOD recap messages into per-trade realized results.

Mike documents EVERY trade (wins + losses) with entry -> exit and a % return in
near-daily recaps. We extract one row per trade line, dedupe, and compute the
honest expectancy. We also reconcile against Mike's own stated per-recap metrics.

Usage:  python3 parse_recaps.py mike
"""
import sqlite3, re, sys, statistics as st
from pathlib import Path

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "mike"
DB = str(Path(__file__).parent / "channels" / CHANNEL / f"{CHANNEL}.db")

pct_re   = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*%')
trade_re = re.compile(r'\b([A-Z]{2,5})?\s*(\d{2,4}(?:\.\d)?)\s*([CP])\b')
arrow_re = re.compile(r'(\d*\.?\d+)\s*(?:→|->)\s*([\d.]+)')


def parse_line(line):
    """Return dict for a trade line, or None."""
    tm = trade_re.search(line)
    if not tm:
        return None
    ticker = (tm.group(1) or 'SPX').upper()   # bare strikes are SPX
    strike = tm.group(2)
    typ = tm.group(3).upper()

    low = line.lower()
    assumed = False
    # determine realized % return
    pm = pct_re.search(line)
    if pm:
        pct = float(pm.group(1))
    elif 'expired worthless' in low or '→ 0' in line or '-100' in low:
        pct = -100.0
    elif any(w in low for w in ['scratch', 'flat', '~be', ' be ', 'breakeven', '| be']):
        pct = 0.0
    elif 'stopped' in low:
        pct, assumed = -50.0, True       # losers w/o stated %: assume a cut/stop loss
    elif 'cut' in low or ('loss' in low and 'lossless' not in low):
        pct, assumed = -40.0, True
    else:
        return dict(skip=True, ticker=ticker, strike=strike, type=typ, line=line.strip())

    am = arrow_re.search(line)
    entry = float(am.group(1)) if am and am.group(1) not in ('', '.') else None
    return dict(skip=False, ticker=ticker, strike=strike, type=typ, pct=pct, assumed=assumed,
                entry=entry, lotto=('lotto' in low), eod=('eod' in low), line=line.strip())


def main():
    c = sqlite3.connect(DB)
    rows = [(ts, t) for ts, t in c.execute(
        "SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp")]
    # dedupe identical messages
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:80]
        if k in seen:
            continue
        seen.add(k); U.append((ts, t))

    recaps = [(ts, t) for ts, t in U if 'recap' in t.lower()]
    # skip the multi-line tree recap (its trades are duplicated in a flat recap same day)
    recaps = [(ts, t) for ts, t in recaps if '├─' not in t and '└─' not in t]

    trades, skipped = [], []
    tkey = set()
    for ts, t in recaps:
        for line in t.split('\n'):
            r = parse_line(line)
            if not r:
                continue
            if r['skip']:
                skipped.append(r['line'])
                continue
            # dedupe identical trade rows across overlapping recaps
            k = (r['ticker'], r['strike'], r['type'], r.get('entry'), r['pct'])
            if k in tkey:
                continue
            tkey.add(k)
            trades.append(r)

    rets = [t['pct'] / 100 for t in trades]
    wins = [r for r in rets if r > 0.001]
    losses = [r for r in rets if r < -0.001]
    flats = [r for r in rets if abs(r) <= 0.001]
    gp, gl = sum(wins), -sum(losses)

    print(f"Parsed {len(trades)} documented trades from {len(recaps)} flat recaps "
          f"({len(skipped)} lines had no clear % and were skipped).\n")
    print(f"  Win rate     : {len(wins)/len(rets)*100:.1f}%  ({len(wins)}W / {len(losses)}L / {len(flats)} flat)")
    print(f"  Avg return   : {st.mean(rets)*100:+.1f}%  per trade   <-- EXPECTANCY (self-reported)")
    print(f"  Median return: {st.median(rets)*100:+.1f}%")
    print(f"  Avg winner   : {st.mean(wins)*100:+.1f}%")
    print(f"  Avg loser    : {st.mean(losses)*100:+.1f}%")
    print(f"  Profit factor: {gp/gl:.2f}")
    print(f"  Best / Worst : {max(rets)*100:+.0f}% / {min(rets)*100:+.0f}%")

    # segment: lotto vs regular
    for label, sub in [("Lotto plays", [t for t in trades if t['lotto']]),
                       ("Non-lotto", [t for t in trades if not t['lotto']])]:
        rr = [t['pct']/100 for t in sub]
        if rr:
            w = sum(1 for x in rr if x > 0.001)
            print(f"\n  {label:<12} n={len(rr):>3}  win {w/len(rr)*100:.0f}%  avg {st.mean(rr)*100:+.1f}%")

    assumed_n = sum(1 for t in trades if t.get('assumed'))
    print(f"\n  ({assumed_n} losers had no stated % -> assumed -40/-50%. {len(skipped)} non-trade lines skipped.)")

    # ---- validate against Mike's OWN stated per-recap metrics ----
    print("\n  VALIDATION vs Mike's own posted tallies:")
    stated = re.compile(r'(\d+)\s*trades?.*?(\d+(?:\.\d+)?)\s*%', re.I | re.S)
    wr_re = re.compile(r'win\s*rate[:\s]*(\d+(?:\.\d+)?)\s*%', re.I)
    for ts, t in recaps:
        wm = wr_re.search(t)
        tm = re.search(r'(\d+)\s*trades', t, re.I)
        if wm or tm:
            print(f"    {ts[:10]}: Mike says "
                  f"{'win '+wm.group(1)+'%' if wm else ''}"
                  f"{'  ('+tm.group(1)+' trades)' if tm else ''}")


if __name__ == "__main__":
    main()
