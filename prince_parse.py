#!/usr/bin/env python3
"""Parse Prince's entries into a trades table (ticker, strike, type, premium, expiry, entry) for
JMT-style reconstruction. Prince format: 'In $XOM $89c July 1 @1.55  s/l 1.20'."""
import sqlite3, re, json
from datetime import date, datetime
from collections import Counter

MON = {m: i for i, m in enumerate(
    ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'], 1)}

def parse():
    c = sqlite3.connect('channels/prince/prince.db')
    rows = list(c.execute('SELECT timestamp, text FROM raw_messages WHERE text IS NOT NULL'))
    out, seen = [], set()
    for ts, m in rows:
        # $TICKER  $STRIKE[c/p]  Month Day  @Premium
        em = re.search(
            r'\$([A-Za-z]{1,5})\s+\$?(\d{1,4}(?:\.\d{1,2})?)\s*([cp])?\b'      # ticker, strike, opt type
            r'[^@\n]*?([A-Za-z]{3,9})\s+(\d{1,2})\b'                            # month, day (expiry)
            r'[^@\n]*?@\s*\$?(\d+\.?\d*)', m, re.I | re.S)                      # premium
        if not em:
            continue
        tk = em.group(1).upper()
        try:
            strike = float(em.group(2))
        except Exception:
            continue
        typ = (em.group(3) or 'c').upper()
        mon = MON.get(em.group(4).lower()[:3])
        if not mon:
            continue
        day = int(em.group(5))
        prem = float(em.group(6))
        if not (0.05 <= prem <= 60) or day > 31:
            continue
        ent = datetime.fromisoformat(ts[:19])
        yr = ent.year + (1 if mon < ent.month else 0)   # expiry rolls to next year if month < entry month
        try:
            exp = date(yr, mon, day)
        except Exception:
            continue
        dte = (exp - ent.date()).days
        if not (0 <= dte <= 60):                          # sane horizon
            continue
        key = (tk, strike, typ, exp.isoformat(), ent.date().isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append({'entry_ts': ts, 'ticker': tk, 'strike': strike, 'type': typ,
                    'premium': prem, 'expiry': exp.isoformat(), 'dte': dte, 'hour': ent.hour})
    return out

if __name__ == '__main__':
    e = parse()
    print(f'parsed {len(e)} Prince trades with full (ticker/strike/type/premium/expiry)')
    print('tickers:', Counter(x['ticker'] for x in e).most_common(15))
    print('dte dist:', Counter(min(x['dte'], 10) for x in e).most_common())
    json.dump(e, open('data/prince_trades.json', 'w'))
    print('-> saved data/prince_trades.json')
    print('sample:', e[0] if e else None)
