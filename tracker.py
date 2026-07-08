#!/usr/bin/env python3
"""
Forward tracker — log a channel's LIVE entry alerts and YOUR actual fills, then
score your real edge vs the channel's documented baseline.

Workflow (daily):
  1. Re-scrape the channel briefly so new alerts are in the DB, reload with load.py.
  2. python3 tracker.py mike sync          # pull new live alerts into the journal
  3. python3 tracker.py mike list          # see what's open
  4. python3 tracker.py mike take <id> 1.55   # you entered at your real fill 1.55
  5. python3 tracker.py mike close <id> 2.10   # you exited at 2.10  (or: close <id> --pct 35)
     python3 tracker.py mike skip <id>          # you didn't take it
  6. python3 tracker.py mike score          # your honest scorecard

The journal is a plain CSV (channels/<ch>/journal.csv) you can also edit by hand.
sync only ADDS new alerts; it never overwrites your entries.
"""
import sqlite3, re, csv, sys, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))
# channel baselines from our historical analysis (documented win% / median return)
BASELINE = {"mike": {"win": 76.0, "median": 61.0},
            "sulker": {"win": 80.0, "median": 65.0}}

COLS = ["id", "alert_ts", "ticker", "strike", "type", "lotto",
        "mike_entry", "status", "my_entry", "my_exit", "my_return_pct", "notes"]

# live alert: optional TICKER, strike, C/P, then a premium (after @ or space). Short msgs only.
alert_re = re.compile(r'^\W*(?:([A-Z]{1,5})\s+)?(\d{2,4}(?:\.\d)?)\s*([cp])\b\s*@?\s*(\d*\.?\d+)?', re.I)


def paths(ch):
    base = Path(__file__).parent / "channels" / ch
    return base / f"{ch}.db", base / "journal.csv"


def to_et(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)


def detect_alerts(db):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT id,timestamp,author,text FROM raw_messages "
                     "WHERE text IS NOT NULL ORDER BY timestamp").fetchall()
    # the provider = the dominant author in the channel (auto-detected, channel-agnostic)
    from collections import Counter
    ac = Counter(a for _, _, a, _ in rows if a and not a.startswith('@'))
    provider = ac.most_common(1)[0][0] if ac else None
    out, recent = [], {}
    for mid, ts, author, text in rows:
        if not author or author != provider:
            continue
        t = text.strip()
        low = t.lower()
        if 'recap' in low or len(t) > 90:           # skip recaps & long commentary
            continue
        m = alert_re.match(t)
        if not m:
            continue
        strike = float(m.group(2))
        if not (5 <= strike <= 9000):
            continue
        # premium: first decimal-formatted number in range (handles "@.55", "| 1.00",
        # "1.85", "at 0.18"); ignores "0DTE" and integer strikes.
        cands = re.findall(r'\d{0,3}\.\d{1,2}', t)
        prem = next((float(x) for x in cands if 0.03 <= float(x) <= 60), None)
        if prem is None:
            continue
        # collapse edited/re-posted duplicates: same trade within 15 min = one alert
        dt = to_et(ts)
        key = (m.group(1) or 'SPX', m.group(2), m.group(3).upper(), prem)
        last = recent.get(key)
        recent[key] = dt
        if last and (dt - last).total_seconds() < 900:
            continue
        out.append(dict(id=mid, alert_ts=dt.isoformat(),
                        ticker=(m.group(1) or 'SPX').upper(), strike=m.group(2),
                        type=m.group(3).upper(), lotto=int('lotto' in low),
                        mike_entry=prem, status='NEW',
                        my_entry='', my_exit='', my_return_pct='', notes=''))
    return out


def read_journal(jp):
    if not jp.exists():
        return {}
    with open(jp, newline='') as f:
        return {r['id']: r for r in csv.DictReader(f)}


def write_journal(jp, rows):
    jp.parent.mkdir(parents=True, exist_ok=True)
    with open(jp, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda x: x['alert_ts']):
            w.writerow({k: r.get(k, '') for k in COLS})


def cmd_sync(ch, args):
    db, jp = paths(ch)
    journal = read_journal(jp)
    added = 0
    for a in detect_alerts(db):
        if a['id'] not in journal:
            journal[a['id']] = a
            added += 1
    write_journal(jp, journal)
    print(f"Synced. {added} new alerts added. Journal now has {len(journal)} rows -> {jp}")
    if added:
        print("Run:  python3 tracker.py", ch, "list")


def short(mid):
    return mid.split('-')[-1][-6:]   # short handle for typing


def cmd_list(ch, args):
    _, jp = paths(ch)
    journal = read_journal(jp)
    shown = 0
    print(f"{'handle':>7}  {'when':<16} {'lot':>3} {'trade':<14} {'mike@':>7} {'status':<8} {'you'}")
    for r in sorted(journal.values(), key=lambda x: x['alert_ts']):
        if args.all or r['status'] in ('NEW', 'TAKEN'):
            you = f"in {r['my_entry']}" if r['my_entry'] else ''
            tag = '🎰' if r['lotto'] == '1' else ''
            print(f"{short(r['id']):>7}  {r['alert_ts'][:16]:<16} {tag:>3} "
                  f"{r['ticker']+' '+r['strike']+r['type']:<14} {r['mike_entry']:>7} {r['status']:<8} {you}")
            shown += 1
    if not shown:
        print("  (nothing open — run sync, or use --all)")


def _find(journal, handle):
    hits = [r for r in journal.values() if short(r['id']) == handle or r['id'] == handle]
    if not hits:
        sys.exit(f"No trade with handle '{handle}'. Run: tracker.py list")
    return hits[0]


def cmd_take(ch, args):
    _, jp = paths(ch); journal = read_journal(jp)
    r = _find(journal, args.handle)
    r['my_entry'] = str(args.price); r['status'] = 'TAKEN'
    write_journal(jp, journal)
    print(f"Recorded your entry {args.price} on {r['ticker']} {r['strike']}{r['type']} (mike @ {r['mike_entry']}).")


def cmd_close(ch, args):
    _, jp = paths(ch); journal = read_journal(jp)
    r = _find(journal, args.handle)
    if args.pct is not None:
        ret = args.pct
    else:
        if not r['my_entry']:
            sys.exit("Set your entry first:  tracker.py take <handle> <price>")
        ret = (args.price / float(r['my_entry']) - 1) * 100
        r['my_exit'] = str(args.price)
    r['my_return_pct'] = f"{ret:.1f}"; r['status'] = 'CLOSED'
    write_journal(jp, journal)
    print(f"Closed {r['ticker']} {r['strike']}{r['type']}: your return {ret:+.1f}%")


def cmd_skip(ch, args):
    _, jp = paths(ch); journal = read_journal(jp)
    r = _find(journal, args.handle); r['status'] = 'SKIPPED'
    write_journal(jp, journal)
    print(f"Marked skipped: {r['ticker']} {r['strike']}{r['type']}")


def _stats(rets):
    import statistics as st
    n = len(rets)
    if not n:
        return None
    wins = [x for x in rets if x > 0.1]
    losses = [x for x in rets if x < -0.1]
    return dict(n=n, win=len(wins)/n*100, mean=st.mean(rets), median=st.median(rets),
                avgw=st.mean(wins) if wins else 0, avgl=st.mean(losses) if losses else 0)


def cmd_score(ch, args):
    _, jp = paths(ch); journal = read_journal(jp)
    closed = [r for r in journal.values() if r['status'] == 'CLOSED' and r['my_return_pct']]
    if not closed:
        print("No closed trades yet. Take some trades and close them first.")
        return
    rets = [float(r['my_return_pct']) for r in closed]
    s = _stats(rets)
    taken = sum(1 for r in journal.values() if r['status'] in ('TAKEN', 'CLOSED'))
    skipped = sum(1 for r in journal.values() if r['status'] == 'SKIPPED')
    print(f"================  YOUR SCORECARD — {ch}  ================")
    print(f"  Closed trades: {s['n']}   (taken {taken}, skipped {skipped})")
    print(f"  Your win rate : {s['win']:.0f}%")
    print(f"  Your median   : {s['median']:+.0f}%   mean {s['mean']:+.0f}%")
    print(f"  Avg win/loss  : {s['avgw']:+.0f}% / {s['avgl']:+.0f}%")
    for label, sub in [("Normal", [r for r in closed if r['lotto'] != '1']),
                       ("Lotto", [r for r in closed if r['lotto'] == '1'])]:
        ss = _stats([float(r['my_return_pct']) for r in sub])
        if ss:
            print(f"    {label:<7} n={ss['n']:>3}  win {ss['win']:.0f}%  median {ss['median']:+.0f}%")
    # slippage vs mike's quoted entry
    sl = [(float(r['my_entry']) / float(r['mike_entry']) - 1) * 100
          for r in closed if r['my_entry'] and r['mike_entry']]
    if sl:
        import statistics as st
        print(f"  Your entry slippage vs Mike's quote: {st.mean(sl):+.1f}% avg "
              f"(positive = you paid more)")
    b = BASELINE.get(ch)
    if b:
        print(f"\n  Mike's documented baseline: win {b['win']:.0f}%, median +{b['median']:.0f}%")
        print(f"  YOUR edge vs his quotes  : win {s['win']-b['win']:+.0f} pts, "
              f"median {s['median']-b['median']:+.0f} pts")
        print("  (this gap IS the real cost of being a follower — the number that matters.)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("channel")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    lp = sub.add_parser("list"); lp.add_argument("--all", action="store_true")
    tp = sub.add_parser("take"); tp.add_argument("handle"); tp.add_argument("price", type=float)
    cp = sub.add_parser("close"); cp.add_argument("handle"); cp.add_argument("price", type=float, nargs='?')
    cp.add_argument("--pct", type=float, default=None)
    kp = sub.add_parser("skip"); kp.add_argument("handle")
    sub.add_parser("score")
    a = p.parse_args()
    {"sync": cmd_sync, "list": cmd_list, "take": cmd_take,
     "close": cmd_close, "skip": cmd_skip, "score": cmd_score}[a.cmd](a.channel, a)


if __name__ == "__main__":
    main()
