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


def _seller_cells(rows):
    """Uncapped {'day|ticker': pnl} over ALL resolved seller positions."""
    agg = defaultdict(float)
    for p in rows:
        d = (p.get("closed") or p.get("day") or "")[:10]
        tk = p.get("ticker")
        if d and tk:
            agg[f"{d}|{tk}"] += float(p.get("pnl") or 0)
    return {k: round(v, 2) for k, v in agg.items()}


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
        # UNCAPPED per-(day,ticker) P&L, mirroring chip_book — `positions` above is capped at 40 for the
        # payload and must never be the pairing's source. Key is "day|ticker" (JSON has no tuple keys).
        "cells": _seller_cells(out),
        "note": "premium seller (long theta) — mirror of chip. Costs real: cross the spread, all-leg "
                "commission. WIN-SMALL / rare-BIG-LOSS shape; 40 calm days will UNDER-sample the tail.",
    }


def paired_vs_chip():
    """The theta experiment's headline metric: seller P&L minus buyer P&L, on IDENTICAL bets.

    PAIRED PER (day, ticker), not per day — and that granularity is the whole validity of the number.
    The two desks do not trade the same universe on a given day: chip (the buyer) trades every ticker,
    but the seller REFUSES the wide-spread names on the day's quote (NVDA, AMZN, META, MSFT were skipped
    on the first live day). Diffing whole-book daily totals therefore compared a 10-ticker buyer against
    a ~6-ticker seller, so the seller scored well partly by never trading the losers — a rigged
    scoreboard, not evidence about theta. Here a cell counts ONLY if BOTH sides traded that ticker that
    day; a ticker one side skipped is dropped from that day's comparison. The result answers the actual
    question: on the same underlying, on the same day, did selling premium beat buying it?

    Reported at two levels: per (day,ticker) CELL, and rolled up to a per-day paired total (the sum of
    that day's shared-ticker diffs). The day remains the bootstrap block for significance; the per-cell
    pairing is what removes the universe-mismatch bias inside each block.
    """
    # BOTH sides read the UNCAPPED `cells` map, never the display-capped trades/positions lists. Consuming
    # the capped list is exactly what made the buyer read -11,948 (last 60 of 180 trades) while the book's
    # own by_day said -49,863. Parse "day|ticker" keys back to (day, ticker) tuples.
    def _parse(cells):
        out = {}
        for k, v in (cells or {}).items():
            d, _, tk = k.partition("|")
            if d and tk:
                out[(d, tk)] = float(v or 0)
        return out

    try:
        import chip_book as CB
        buyer_cell = _parse(CB.replay().get("cells"))
    except Exception:
        buyer_cell = {}
    seller_cell = _parse(replay().get("cells"))

    # SHARED cells only: both sides traded that ticker that day.
    shared = sorted(set(seller_cell) & set(buyer_cell))
    cells = [{"day": d, "ticker": tk,
              "seller": round(seller_cell[(d, tk)], 2), "buyer": round(buyer_cell[(d, tk)], 2),
              "diff": round(seller_cell[(d, tk)] - buyer_cell[(d, tk)], 2)} for d, tk in shared]

    # What each side traded that the OTHER did not — surfaced, not silently dropped, so the operator can
    # see how much of each book is EXCLUDED from the comparison and whether the pairing is starving.
    seller_only = sorted(set(seller_cell) - set(buyer_cell))
    buyer_only = sorted(set(buyer_cell) - set(seller_cell))

    # Roll shared cells up to a paired daily total; the day is still the block for significance.
    day_agg = defaultdict(lambda: {"seller": 0.0, "buyer": 0.0, "diff": 0.0, "tickers": 0})
    for c in cells:
        a = day_agg[c["day"]]
        a["seller"] += c["seller"]; a["buyer"] += c["buyer"]; a["diff"] += c["diff"]; a["tickers"] += 1
    series = [{"day": d, "seller": round(v["seller"], 2), "buyer": round(v["buyer"], 2),
               "diff": round(v["diff"], 2), "tickers": v["tickers"]} for d, v in sorted(day_agg.items())]

    n_days = len(series)
    mean_daily_diff = round(sum(x["diff"] for x in series) / n_days, 2) if n_days else None
    mean_cell_diff = round(sum(c["diff"] for c in cells) / len(cells), 2) if cells else None
    return {"days": n_days, "cells": len(cells), "mean_daily_diff": mean_daily_diff,
            "mean_cell_diff": mean_cell_diff, "series": series, "cell_series": cells[-60:],
            "unpaired": {"seller_only": len(seller_only), "buyer_only": len(buyer_only)},
            "note": "seller - buyer, PAIRED per (day,ticker): only cells BOTH sides traded count, so the "
                    "seller refusing a wide name no longer flatters the diff. Day is the block; needs "
                    "many independent days for a confident sign, a handful is directional only."}


if __name__ == "__main__":
    b = replay()
    print(f"seller book  ${b['equity']:,.2f}  ({b['pnl']:+,.2f}, {b['return_pct']:+.2f}%)  "
          f"n={b['n']} win {b['win_rate']}%  dd {b['drawdown_pct']}%")
    for d in b["by_day"]:
        print(f"   {d['period']}  ${d['pnl']:+9,.2f}  n={d['n']}  win {d['win_rate']}%")
    print("paired vs chip:", paired_vs_chip())
