#!/usr/bin/env python3
"""
DOUBLE-CHECK / CROSS-CHECK of the Mike analysis. Looks for ways the edge could be
overstated by MY parser or by HIS reporting:
  1. Reconcile my parsed win-rate vs Mike's OWN stated tallies (per recap).
  2. Day coverage: does he recap every trading day, or cherry-pick good ones?
  3. Within-recap completeness: my parsed count vs his stated "Total Trades: N".
  4. Multi-trim overstatement: are reported %s the PEAK trim, not blended?
  5. Edge stability over the 16 months (front-loaded? decaying?).
Run: .venv/bin/python mike_crosscheck.py
"""
import sqlite3, re, statistics as st
from pathlib import Path
from collections import defaultdict
import numpy as np

DB = str(Path(__file__).parent / "channels" / "mike" / "mike.db")
pct_re = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*%')
trade_re = re.compile(r'\b([A-Z]{2,5})?\s*(\d{2,4}(?:\.\d)?)\s*([CP])\b')
arrow_re = re.compile(r'(\d*\.?\d+)\s*(?:→|->)\s*([\d.]+)')


def parse_line(line):
    tm = trade_re.search(line)
    if not tm: return None
    low = line.lower()
    pm = pct_re.search(line)
    if pm:                                                pct = float(pm.group(1))
    elif 'expired worthless' in low or '-100' in low:     pct = -100.0
    elif any(w in low for w in ['scratch','flat','~be','breakeven']): pct = 0.0
    elif 'stopped' in low:                                pct = -50.0
    elif 'cut' in low or 'loss' in low:                   pct = -40.0
    else:                                                 return None
    trims = max(line.count('/') + line.count('→') - 1, 0)
    return dict(pct=max(pct,-100.0), trims=trims)


def dedupe(rows):
    seen, U = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+',' ',t.strip().lower())[:80]
        if k in seen: continue
        seen.add(k); U.append((ts, t))
    return U


def main():
    c = sqlite3.connect(DB)
    allrows = dedupe(c.execute("SELECT timestamp,text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp").fetchall())
    recaps = [(ts, t) for ts, t in allrows if 'recap' in t.lower() and '├─' not in t]

    # ---------- 1 & 3: per-recap reconciliation ----------
    stated_wr = re.compile(r'win\s*rate[:\s]*(\d+(?:\.\d+)?)\s*%', re.I)
    stated_n  = re.compile(r'total trades[:\s]*(\d+)', re.I)
    wl1 = re.compile(r'(\d+)\s*W\s*/\s*(\d+)\s*(?:scratch|be|/)?\s*\D*?(\d+)?\s*L', re.I)
    wl2 = re.compile(r'(\d+)\s*green\D+(\d+)\s*red', re.I)
    wl3 = re.compile(r'winners?:?\s*(\d+)\D+losers?:?\s*(\d+)', re.I)

    rows_cmp = []   # (date, my_n, my_win%, his_n, his_win%)
    my_all, his_all = [], []
    for ts, t in recaps:
        mine = [parse_line(l) for l in t.split('\n')]
        mine = [m for m in mine if m]
        if not mine: continue
        my_n = len(mine); my_w = sum(1 for m in mine if m['pct'] > 0.001)
        my_wr = my_w / my_n * 100

        his_n = int(stated_n.search(t).group(1)) if stated_n.search(t) else None
        his_wr = float(stated_wr.search(t).group(1)) if stated_wr.search(t) else None
        if his_wr is None:
            for rx in (wl3, wl2, wl1):
                m = rx.search(t)
                if m:
                    w = int(m.group(1)); l = int(m.group(2) or 0)
                    if w + l > 0: his_wr = w/(w+l)*100; his_n = his_n or (w+l)
                    break
        rows_cmp.append((ts[:10], my_n, my_wr, his_n, his_wr))
        if his_wr is not None:
            my_all.append(my_wr); his_all.append(his_wr)

    print("================  CROSS-CHECK 1: MY PARSE vs HIS STATED  ================")
    paired = [(d, mn, mw, hn, hw) for d, mn, mw, hn, hw in rows_cmp if hw is not None]
    print(f"  Recaps where he stated a win rate: {len(paired)}")
    if paired:
        diffs = [mw - hw for _, _, mw, _, hw in paired]
        print(f"  My win% avg: {st.mean([p[2] for p in paired]):.1f}%   "
              f"His stated avg: {st.mean([p[4] for p in paired]):.1f}%")
        print(f"  Mean(my - his): {st.mean(diffs):+.1f} pts   median diff: {st.median(diffs):+.1f} pts")
        print(f"  -> If positive, MY parse is too optimistic (dropping losers).")
        ncmp = [(d, mn, hn) for d, mn, mw, hn, hw in rows_cmp if hn]
        cntdiff = [mn - hn for _, mn, hn in ncmp]
        print(f"  Recaps with his trade-count: {len(ncmp)}   "
              f"mean(my_n - his_n): {st.mean(cntdiff):+.1f}  (negative = I'm MISSING some of his trades)")
        print("  worst mismatches (date, my_n/my_win%, his_n/his_win%):")
        for d, mn, mw, hn, hw in sorted(paired, key=lambda r: -abs(r[2]-r[4]))[:6]:
            print(f"    {d}  me {mn}t/{mw:.0f}%   him {hn}t/{hw:.0f}%")

    # ---------- 2: day coverage (cherry-picking?) ----------
    print("\n================  CROSS-CHECK 2: DAY COVERAGE  ================")
    recap_days = set(ts[:10] for ts, _ in recaps)
    # live entry-ish messages from Mike: a strike+C/P plus a small premium
    entry_days = set()
    for ts, t in allrows:
        if re.search(r'\b\d{2,5}\s*[cp]\b', t, re.I) and re.search(r'\b\d{0,2}\.\d{1,2}\b', t) and len(t) < 80:
            entry_days.add(ts[:10])
    all_days = set(ts[:10] for ts, _ in allrows)
    print(f"  Distinct days with ANY message:   {len(all_days)}")
    print(f"  Distinct days he posted trades:   {len(entry_days)}")
    print(f"  Distinct days he posted a RECAP:  {len(recap_days)}")
    only_traded = entry_days - recap_days
    print(f"  Days he traded but did NOT recap: {len(only_traded)}  "
          f"({len(only_traded)/max(len(entry_days),1)*100:.0f}% of trading days unrecapped)")
    print("  -> Unrecapped days are INVISIBLE to our edge. If he skips bad days, edge is overstated.")

    # ---------- 4: multi-trim overstatement ----------
    print("\n================  CROSS-CHECK 4: TRIM OVERSTATEMENT  ================")
    allt = [parse_line(l) for _, t in recaps for l in t.split('\n')]
    allt = [m for m in allt if m]
    multi = [m for m in allt if m['trims'] >= 2 and m['pct'] > 0]
    single = [m for m in allt if m['trims'] < 2 and m['pct'] > 0]
    print(f"  Winning trades with multiple trims (e.g. '1.5/2.4/3.6'): {len(multi)} of {len([m for m in allt if m['pct']>0])} wins")
    if multi and single:
        print(f"  Median % on multi-trim wins: {np.median([m['pct'] for m in multi]):+.0f}%  "
              f"vs single-exit wins: {np.median([m['pct'] for m in single]):+.0f}%")
        print("  -> The headline % is the BEST trim; only part of the position got it. Blended is lower.")

    # ---------- 5: edge stability over time ----------
    print("\n================  CROSS-CHECK 5: EDGE STABILITY BY MONTH  ================")
    bym = defaultdict(list)
    for ts, t in recaps:
        for l in t.split('\n'):
            m = parse_line(l)
            if m: bym[ts[:7]].append(m['pct']/100)
    print(f"  {'month':<9}{'n':>5}{'win%':>7}{'median':>9}")
    for mo in sorted(bym):
        r = np.array(bym[mo])
        if len(r) >= 8:
            print(f"  {mo:<9}{len(r):>5}{(r>0.001).mean()*100:>6.0f}%{np.median(r)*100:>+8.0f}%")


if __name__ == "__main__":
    main()
