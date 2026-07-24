#!/usr/bin/env python3
"""
seller_book.py — the SECOND $50k paper book: the premium SELLER (long theta), run alongside chip (the
buyer). Same measurement discipline as chip_book, mirror-image strategy. Two arms are scored separately
(SME: don't confound them):
  · arm1 — directional credit spread on chip's EMA side  (isolates the theta sign)
  · arm2 — neutral 0DTE iron condor                       (pure short-vol)

Resolved positions are appended to channels/seller/positions.jsonl by seller_desk. Each carries its own
cost-honest realized P&L (credit collected, bought back cost, all-leg commission). The book just rolls
them into an equity curve and — the key SME ask — computes the PAIRED DAILY difference vs chip, because
pairing on the shared tape cancels the market-regime variance that would otherwise swamp 40 days.

Nothing is deleted; positions.jsonl is append-only and retained.
"""
import json
from collections import defaultdict
from datetime import datetime

import deskpaths as _P

START_EQUITY = 250_000.0        # matched to chip's book so the paired comparison is like-for-like
MEASUREMENT_START = "2026-07-23"          # same clean-window convention as chip_book
STORE = _P.CHANNELS / "seller" / "positions.jsonl"


def _load():
    try:
        return [json.loads(ln) for ln in STORE.read_text().splitlines() if ln.strip()]
    except Exception:
        return []


def record(pos):
    """Append one RESOLVED seller position. Called by seller_desk when a position closes."""
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        with STORE.open("a") as f:
            f.write(json.dumps(pos) + "\n")
        return True
    except Exception:
        return False


def _period(day, kind):
    if not day or len(day) < 10:
        return "?"
    if kind == "month":
        return day[:7]
    if kind == "week":
        try:
            y, w, _ = datetime.strptime(day, "%Y-%m-%d").isocalendar()
            return f"{y}-W{w:02d}"
        except Exception:
            return "?"
    return day


def replay(arm=None):
    """Roll resolved positions into the $50k curve. arm=None => both; 'arm1'/'arm2' => that arm only."""
    rows = [p for p in _load()
            if (p.get("closed") or p.get("day") or "")[:10] >= MEASUREMENT_START
            and (arm is None or p.get("arm") == arm)]
    rows.sort(key=lambda p: (p.get("closed") or p.get("day") or ""))
    eq = peak = START_EQUITY
    daily = defaultdict(float)
    out = []
    for p in rows:
        pnl = float(p.get("pnl") or 0)
        eq += pnl
        peak = max(peak, eq)
        day = (p.get("closed") or p.get("day") or "")[:10]
        daily[day] += pnl
        out.append({**p, "equity_after": round(eq, 2)})
    n = len(out)
    wins = [p for p in out if float(p.get("pnl") or 0) > 0]
    by = {}
    for kind in ("day", "week", "month"):
        agg = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})
        for p in out:
            k = _period((p.get("closed") or p.get("day") or "")[:10], kind)
            agg[k]["pnl"] += float(p.get("pnl") or 0)
            agg[k]["n"] += 1
            if float(p.get("pnl") or 0) > 0:
                agg[k]["wins"] += 1
        by[kind] = [{"period": k, "pnl": round(v["pnl"], 2), "n": v["n"], "wins": v["wins"],
                     "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else None}
                    for k, v in sorted(agg.items())]
    return {
        "start": START_EQUITY, "equity": round(eq, 2), "peak": round(peak, 2),
        "pnl": round(eq - START_EQUITY, 2), "return_pct": round((eq / START_EQUITY - 1) * 100, 2),
        "drawdown_pct": round((eq / peak - 1) * 100, 2) if peak else 0.0,
        "n": n, "wins": len(wins), "win_rate": round(100.0 * len(wins) / n, 1) if n else None,
        "by_day": by["day"], "by_week": by["week"], "by_month": by["month"],
        "positions": out[-40:], "daily": {d: round(v, 2) for d, v in sorted(daily.items())},
        "note": "premium seller (long theta) — mirror of chip. Costs real: cross the spread, all-leg "
                "commission. WIN-SMALL / rare-BIG-LOSS shape; 40 calm days will UNDER-sample the tail.",
    }


def paired_vs_chip():
    """The SME's headline metric: daily (seller - buyer), day-blocked. Pairing on the shared tape cancels
    the regime variance, which is the only way 40 days has power. Returns the per-day diff series + mean."""
    seller = replay()["daily"]
    try:
        import chip_book as CB
        chip_daily = defaultdict(float)
        for r in CB.replay().get("trades", []):
            chip_daily[(r.get("day") or "")[:10]] += float(r.get("pnl") or 0)
    except Exception:
        chip_daily = {}
    days = sorted(set(seller) | set(chip_daily))
    diffs = [{"day": d, "seller": round(seller.get(d, 0.0), 2), "buyer": round(chip_daily.get(d, 0.0), 2),
              "diff": round(seller.get(d, 0.0) - chip_daily.get(d, 0.0), 2)} for d in days]
    n = len(diffs)
    mean_diff = round(sum(x["diff"] for x in diffs) / n, 2) if n else None
    return {"days": n, "mean_daily_diff": mean_diff, "series": diffs,
            "note": "seller - buyer per shared day; day is the block. Needs many independent days for a "
                    "confident sign; a handful is directional only."}


if __name__ == "__main__":
    b = replay()
    print(f"seller book  ${b['equity']:,.2f}  ({b['pnl']:+,.2f}, {b['return_pct']:+.2f}%)  "
          f"n={b['n']} win {b['win_rate']}%  dd {b['drawdown_pct']}%")
    for d in b["by_day"]:
        print(f"   {d['period']}  ${d['pnl']:+9,.2f}  n={d['n']}  win {d['win_rate']}%")
    print("paired vs chip:", paired_vs_chip())
