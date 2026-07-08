#!/usr/bin/env python3
"""
Forward-validation — the SME's #1 tool. Measures whether the edge is REAL on YOUR
fills, once you've logged live trades with the tracker/dashboard.

Reports, from your journals:
  - Slippage: your fill vs the provider's quoted price (the silent P&L killer).
  - Edge by confidence tier and by tape read (ALIGNED vs AGAINST) — do the filters help?
  - Per-provider realized expectancy over the last 20 trades (kill-switch signal).

Run:  python3 validate.py     (needs closed trades logged first)
"""
import csv, re, statistics as st
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent


def rows_for(ch):
    jp = ROOT / "channels" / ch / "journal.csv"
    if not jp.exists():
        return []
    out = []
    for r in csv.DictReader(open(jp)):
        r["_conf"] = int(m.group(1)) if (m := re.search(r"conf=(\d+)", r.get("notes", "") or "")) else None
        r["_tape"] = (m.group(1) if (m := re.search(r"tape=(\S+)", r.get("notes", "") or "")) else None)
        out.append(r)
    return out


def killed_providers(window=20):
    """Providers whose realized expectancy over the last `window` closed trades (on YOUR
    fills) is negative — auto-disable per the SME kill-switch. Needs a full window first."""
    byp = defaultdict(list)
    for ch in [p.name for p in (ROOT / "channels").glob("*") if (p / "journal.csv").exists()]:
        for r in rows_for(ch):
            if r.get("status") == "CLOSED" and r.get("my_return_pct"):
                byp[ch].append(float(r["my_return_pct"]))
    killed = {}
    for p, rr in byp.items():
        if len(rr) >= window and st.mean(rr[-window:]) < 0:
            killed[p] = round(st.mean(rr[-window:]), 1)
    return killed


def graduation_check(prov, min_n=40, slippage_R=0.15):
    """B4 gate: should a PAPER provider (mike/optionking) graduate to live trading?
    Reads channels/<prov>/paper_journal.csv (your simulated managed closes). Manual-promote only.
    Passes iff: >= min_n closes, mean R beats slippage-breakeven, losers actually cut (no -100 zeros)."""
    jp = ROOT / "channels" / prov / "paper_journal.csv"
    closes = []
    if jp.exists():
        for r in csv.DictReader(open(jp)):
            if r.get("status") == "CLOSED" and r.get("my_return_pct"):
                closes.append(float(r["my_return_pct"]) / 35.0)   # % -> R (1R = 35% sizing stop)
    n = len(closes)
    if n == 0:
        return {"provider": prov, "n": 0, "pass": False, "reason": "no paper closes logged yet"}
    mean_R = round(st.mean(closes), 2)
    worst_R = round(min(closes), 2)
    losers = [r for r in closes if r < 0]
    cut = sum(1 for r in losers if r >= -1.0) / len(losers) if losers else 1.0   # -1R ~ -35%
    ok = (n >= min_n) and (mean_R > slippage_R) and (cut >= 0.90) and (worst_R >= -1.0)
    return {"provider": prov, "n": n, "mean_R": mean_R, "worst_R": worst_R,
            "pct_losers_cut": round(cut * 100), "pass": ok,
            "reason": None if ok else
            (f"only {n}/{min_n} closes" if n < min_n else
             f"mean {mean_R}R ≤ slippage {slippage_R}R" if mean_R <= slippage_R else
             f"losers-cut {round(cut*100)}% < 90%" if cut < 0.90 else
             f"a loser hit {worst_R}R (≤ -1R) — stop not holding")}


def main():
    providers = [p.name for p in (ROOT / "channels").glob("*") if (p / "journal.csv").exists()]
    allrows = []
    for ch in providers:
        for r in rows_for(ch):
            r["_prov"] = ch
            allrows.append(r)
    closed = [r for r in allrows if r.get("status") == "CLOSED" and r.get("my_return_pct")]

    print("=" * 60)
    print(f"FORWARD VALIDATION — {len(closed)} closed trades logged")
    print("=" * 60)
    if not closed:
        print("\nNo closed trades yet. Take & close trades on the dashboard first,")
        print("then this proves whether the edge survives YOUR real fills.")
        return

    # 1. slippage vs provider quote
    slip = [(float(r["my_entry"]) / float(r["mike_entry"]) - 1) * 100
            for r in closed if r.get("my_entry") and r.get("mike_entry")
            and float(r.get("mike_entry") or 0) > 0]
    if slip:
        print(f"\nSLIPPAGE (your fill vs quote): mean {st.mean(slip):+.1f}%  median {st.median(slip):+.1f}%")
        print("  (positive = you paid up. >3-5% round-trip can flip the edge negative.)")

    def bucket(label, groups):
        print(f"\n{label}")
        for name, rs in groups.items():
            rr = [float(x["my_return_pct"]) for x in rs]
            if rr:
                w = sum(1 for v in rr if v > 1) / len(rr) * 100
                print(f"  {str(name):<14} n={len(rr):>3}  win {w:>3.0f}%  mean {st.mean(rr):>+5.0f}%  median {st.median(rr):>+5.0f}%")

    # 2. by tape read — does the technical filter help?
    tp = defaultdict(list)
    for r in closed:
        tp[r["_tape"] or "n/a"].append(r)
    bucket("EDGE BY TAPE READ (does VWAP/RSI/BB filter help?):", tp)

    # 3. by confidence tier
    ct = defaultdict(list)
    for r in closed:
        c = r["_conf"]
        tier = "n/a" if c is None else "A+(80+)" if c >= 80 else "B(65-79)" if c >= 65 else "C(<65)"
        ct[tier].append(r)
    bucket("EDGE BY CONFIDENCE TIER:", ct)

    # 4. per-provider rolling-20 expectancy (kill switch)
    print("\nPER-PROVIDER — last 20 trades (kill switch if negative):")
    byp = defaultdict(list)
    for r in closed:
        byp[r["_prov"]].append(float(r["my_return_pct"]))
    for p, rr in byp.items():
        last = rr[-20:]
        exp = st.mean(last)
        print(f"  {p:<10} last {len(last):>2}: exp {exp:>+5.0f}%  {'⚠ DISABLE — negative' if exp < 0 else 'ok'}")


if __name__ == "__main__":
    main()
