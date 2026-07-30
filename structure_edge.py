#!/usr/bin/env python3
"""
structure_edge.py — does entry-time MARKET STRUCTURE predict the outcome? The mining tool for the
struct_* features, built to answer the owner's price-action thesis with evidence instead of assertion:

    "A displacement into premium with no defended higher low should lose more."

TWO SME PRINCIPLES ARE BAKED IN, because getting them wrong would make every number a lie:

1. SLICE BY PROVIDER, NEVER POOL. chip reads structure on 1-min bars (a 20-min scalp); the humans read
   it on 5-min bars (swing/day watches). struct_premium_pct=0.8 does NOT mean the same thing for a chip
   scalp and a prince swing — pooling them regresses noise against signal. Every stat here is per
   provider. A cross-provider view is offered ONLY as "compare the patterns", never one merged model.

2. STRUCTURE IS SIDE-RELATIVE. "Into premium" is a headwind for a CALL (buying into supply/resistance)
   but a TAILWIND for a PUT (selling into supply). Same for the defended pullback: a bull wants a higher
   low held; a bear wants a lower high held. So the raw columns are re-expressed relative to the trade's
   own direction before testing:
     · into_opposing  = premium_pct for calls, (1 - premium_pct) for puts   [1 = bought into the wall]
     · aligned_disp   = displacement in the trade's direction (intent, but note WHERE — see composite)
     · defended       = higher_low for calls, lower_high for puts            [the retest held]
   The owner's thesis, expressed here: high into_opposing + aligned_disp + not-defended => worse.

STATISTICS. The unit of independence is the DAY, not the trade (scalps within a session are heavily
correlated). Effect = difference in mean return% between the "risky" structure and the rest, with a
DAY-BLOCK bootstrap CI. A CI that excludes 0 is a finding; otherwise it is reported as "not yet
separable" — with the honest sample count, because in the first weeks most cells will be thin.

Run:  python3 structure_edge.py            all providers
      python3 structure_edge.py chip       one provider
"""
import sqlite3
import statistics as st
import sys
from collections import defaultdict

import db as DB

PROVIDERS = ("chip", "prince", "prince_small", "optionking", "jmt")
MIN_DAYS = 3          # below this a day-block CI is meaningless; report counts only
MIN_CELL = 8          # a group smaller than this is "thin", flagged not concluded


def _trades(con, provider):
    """Resolved trades with structure + outcome + the day (block). One row per trade."""
    q = """SELECT s.type, o.return_pct, o.result, s.struct_state, s.struct_bos, s.struct_higher_low,
                  s.struct_lower_high, s.struct_displacement, s.struct_disp_atr, s.struct_premium_pct,
                  substr(COALESCE(o.closed_utc,o.opened_utc,s.ts_utc),1,10) AS day
           FROM signals s JOIN outcomes o ON o.signal_id = s.signal_id
           WHERE s.provider = ? AND o.return_pct IS NOT NULL AND s.struct_premium_pct IS NOT NULL"""
    out = []
    for r in con.execute(q, (provider,)):
        typ, ret, res, state, bos, hl, lh, disp, disp_atr, prem, day = r
        is_call = str(typ or "C").upper() != "P"
        out.append({
            "is_call": is_call, "ret": float(ret), "win": float(ret) > 0, "day": day,
            "state": state, "bos": bos, "disp": disp,
            # side-relative derivations
            "into_opposing": prem if is_call else (1 - prem),
            "aligned_disp": (disp == "bull") if is_call else (disp == "bear"),
            "defended": (hl == 1) if is_call else (lh == 1),
        })
    return out


def _block_boot(days_a, days_b, n=3000, seed=7):
    """Day-block bootstrap of (mean_a - mean_b). days_* are lists-of-lists (per day). Returns
    (diff, lo, hi) or None if too few day-blocks on either side."""
    import random
    da = [d for d in days_a if d]
    db_ = [d for d in days_b if d]
    if len(da) < MIN_DAYS or len(db_) < MIN_DAYS:
        return None
    fa = [x for d in da for x in d]
    fb = [x for d in db_ for x in d]
    diff = sum(fa) / len(fa) - sum(fb) / len(fb)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        pa = [x for _ in da for x in da[rng.randrange(len(da))]]
        pb = [x for _ in db_ for x in db_[rng.randrange(len(db_))]]
        if pa and pb:
            diffs.append(sum(pa) / len(pa) - sum(pb) / len(pb))
    diffs.sort()
    return round(diff, 2), round(diffs[int(0.025 * len(diffs))], 2), round(diffs[int(0.975 * len(diffs))], 2)


def _split(trades, pred):
    """Split into (risky, rest), each as {mean_ret, win, n} + per-day lists for the bootstrap."""
    risky, rest = [t for t in trades if pred(t)], [t for t in trades if not pred(t)]

    def stat(g):
        if not g:
            return {"n": 0, "win": None, "mean": None}
        return {"n": len(g), "win": round(100 * sum(x["win"] for x in g) / len(g), 1),
                "mean": round(sum(x["ret"] for x in g) / len(g), 2)}

    def by_day(g):
        d = defaultdict(list)
        for x in g:
            d[x["day"]].append(x["ret"])
        return list(d.values())
    return stat(risky), stat(rest), by_day(risky), by_day(rest)


def _line(name, trades, pred):
    ra, re, da, de = _split(trades, pred)
    boot = _block_boot(da, de)
    verdict = "thin sample" if (ra["n"] < MIN_CELL or re["n"] < MIN_CELL) else (
        "not yet separable" if boot is None else
        (f"REAL: risky {boot[0]:+} vs rest [{boot[1]:+}, {boot[2]:+}]" if (boot[1] > 0) == (boot[2] > 0)
         else f"noise: {boot[0]:+} [{boot[1]:+}, {boot[2]:+}]"))
    rw = "—" if ra["win"] is None else f"{ra['win']}%"
    ew = "—" if re["win"] is None else f"{re['win']}%"
    rm = "—" if ra["mean"] is None else f"{ra['mean']:+}"
    em = "—" if re["mean"] is None else f"{re['mean']:+}"
    print(f"    {name:26s} risky n={ra['n']:3d} win {rw:>5s} ret {rm:>7s}  |  rest n={re['n']:3d} "
          f"win {ew:>5s} ret {em:>7s}   {verdict}")


def report(providers):
    con = sqlite3.connect(str(DB.DB_PATH))
    for prov in providers:
        trades = _trades(con, prov)
        days = len({t["day"] for t in trades})
        print(f"\n{'='*100}\n{prov.upper()}  —  {len(trades)} resolved trades with structure over {days} day(s)")
        if not trades:
            print("    no trades with structure yet"); continue
        print(f"{'='*100}")
        # single-feature tests (side-relative)
        _line("bought INTO the wall", trades, lambda t: t["into_opposing"] >= 0.67)
        _line("aligned displacement", trades, lambda t: t["aligned_disp"])
        _line("retest NOT defended", trades, lambda t: not t["defended"])
        _line("in a range (no trend)", trades, lambda t: t["state"] == "range")
        # THE OWNER'S COMPOSITE THESIS
        _line("THESIS: disp→premium, no HL", trades,
              lambda t: t["aligned_disp"] and t["into_opposing"] >= 0.67 and not t["defended"])
    con.close()
    print(f"\n{'='*100}")
    print("risky = the group the price-action read flags as worse. A finding needs the CI to exclude 0")
    print(f"AND both cells >= {MIN_CELL} trades over >= {MIN_DAYS} days. Early on, expect 'thin'/'not yet'.")
    print("Read per-provider — chip's 1-min structure and the humans' 5-min structure are NOT the same unit.")


if __name__ == "__main__":
    provs = [a for a in sys.argv[1:] if a in PROVIDERS] or list(PROVIDERS)
    report(provs)
