#!/usr/bin/env python3
"""
Parse raw_messages -> a clean `trades` table.

Each entry signal looks like (single or multi-line):
    386P 3.02 6/29
    SWING*** 379P 1.09 6/29
    6560C 1.28 ... SL:751.74
    355p \n 2.10>2.15 \n 10/12          (arrow = entry>exit, we take the ENTRY=2.10)

We extract: entry datetime, strike, type (C/P), entry premium, expiry date (year inferred),
optional stop-loss (underlying level). Rows missing strike/premium/expiry are skipped.
"""
import sqlite3, re, sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

ET = timezone(timedelta(hours=-4))
CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "jmt"
DB = str(Path(__file__).parent / "channels" / CHANNEL / f"{CHANNEL}.db")

strike_re = re.compile(r'^\W*(?:swing\W*)?(\d{3,4})\s*([cp])\b', re.I)   # SPY strikes ~200-800
prem_re   = re.compile(r'(\d{0,2}\.\d{1,2})')
arrow_re  = re.compile(r'(\d{0,2}\.\d{1,2})\s*>\s*\d')
exp_re    = re.compile(r'\b(\d{1,2})/(\d{1,2})\b')
sl_re     = re.compile(r'sl[:\s]*\$?(\d{3,4}(?:\.\d+)?)', re.I)


def to_et(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)


def infer_expiry(entry_dt, month, day):
    for yr in (entry_dt.year, entry_dt.year + 1):
        try:
            e = date(yr, month, day)
        except ValueError:
            return None
        if e >= entry_dt.date() - timedelta(days=1):  # expiry on/after entry day
            # sanity: SPY signals expire within ~3 weeks
            if (e - entry_dt.date()).days <= 25:
                return e
    return None


def parse_entry(ts, text):
    sm = strike_re.match(text)
    if not sm:
        return None
    strike = int(sm.group(1))
    if not (150 <= strike <= 900):
        return None
    typ = sm.group(2).upper()

    # premium: prefer the entry side of an arrow, else first plausible decimal
    am = arrow_re.search(text)
    if am and am.group(1):
        prem = float(am.group(1))
    else:
        prem = None
        for m in prem_re.finditer(text):
            v = float(m.group(1))
            if 0.05 <= v <= 30:
                prem = v
                break
    if prem is None:
        return None

    em = exp_re.search(text)
    if not em:
        return None
    entry_dt = to_et(ts)
    expiry = infer_expiry(entry_dt, int(em.group(1)), int(em.group(2)))
    if expiry is None:
        return None

    slm = sl_re.search(text)
    sl = float(slm.group(1)) if slm else None

    dte = (expiry - entry_dt.date()).days
    return dict(entry_ts=entry_dt.isoformat(), strike=strike, type=typ, premium=prem,
                expiry=expiry.isoformat(), sl=sl, dte=dte, hour=entry_dt.hour, text=text[:120])


def main():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT timestamp, text FROM raw_messages WHERE text IS NOT NULL ORDER BY timestamp").fetchall()

    # de-dupe near-identical (edited messages captured twice)
    seen, uniq = set(), []
    for ts, t in rows:
        k = re.sub(r'\s+', ' ', t.strip().lower())[:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append((ts, t))

    trades = [p for ts, t in uniq if (p := parse_entry(ts, t))]

    conn.execute("DROP TABLE IF EXISTS trades")
    conn.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, entry_ts TEXT, strike INT, type TEXT, premium REAL,
        expiry TEXT, sl REAL, dte INT, hour INT, text TEXT)""")
    conn.executemany("""INSERT INTO trades (entry_ts,strike,type,premium,expiry,sl,dte,hour,text)
        VALUES (:entry_ts,:strike,:type,:premium,:expiry,:sl,:dte,:hour,:text)""", trades)
    conn.commit()

    print(f"Parsed {len(trades)} clean trades (of {len(uniq)} unique messages).")
    with_sl = sum(1 for t in trades if t['sl'])
    print(f"  with explicit SL: {with_sl}")
    print(f"  DTE distribution: " + ", ".join(
        f"{d}d={sum(1 for t in trades if t['dte']==d)}" for d in range(0, 8)))
    print("\n  sample:")
    for t in trades[:6]:
        print(f"   {t['entry_ts'][:16]}  {t['strike']}{t['type']}  ${t['premium']:.2f}  "
              f"exp {t['expiry']} (DTE {t['dte']})  SL={t['sl']}")


if __name__ == "__main__":
    main()
