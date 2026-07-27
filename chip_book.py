#!/usr/bin/env python3
"""
chip_book.py — a REALISTIC paper trading account for chip. Contracts, dollars, and a real P&L.

The scalp log showed R-multiples, which are abstract: "+0.34R" does not tell you whether you made $40 or
$400, how many contracts were on, or what the account is worth. This is the account. It sizes positions off
CURRENT equity, records the actual fill, manages the position through the scale-out ladder, charges the
real round trip, and reports P&L by day, week and month in dollars.

HOW A POSITION IS SIZED — measurement, not appetite:
    risk_budget = equity * RISK_PCT/100          (1% of the CURRENT book, so it compounds and decays)
    risk_per_ct = stop_pct/100 * entry * 100     (the $ lost if the stop trades)
    contracts   = floor(risk_budget / risk_per_ct)
  If that is zero the trade is REFUSED — the instrument is too big for the book, which is a real answer,
  not an obstacle to route around. (This is the bug that made chip silently SPY-only for two days.)

WHAT THIS BOOK WILL ALMOST CERTAINLY DO: lose money, slowly. chip's entries measure no better than random
on this desk's own harness, and the round trip is ~0.056R per trade. At ~6 trades/day that is roughly
-$85/day on $50k. The book exists to show that HONESTLY, in dollars, rather than to flatter it — a paper
account that only ever gets shown when it is green is a marketing device, not an instrument.

COSTS ARE REAL: entry at the ASK, exit at the BID, $1.30/contract round trip (tastytrade $1 to open + fees,
$0 to close). Nothing here is mid-to-mid.
"""
import json, sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import deskpaths as _P


def _secs_between(a, b):
    """Whole seconds between two ISO timestamps, or None if either is missing/unparseable."""
    if not a or not b:
        return None
    try:
        return max(0, int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()))
    except Exception:
        return None

# BUDGET RAISED 50k -> 250k when the universe went to 10 tickers (owner's call, 2026-07-24).
# The binding reason is NOT risk appetite, it is EXECUTABILITY of the runner discipline: at $50k the
# most expensive ticker (TSLA, ~$130 risk/contract) sized to just 3 contracts, and exec_plan degrades
# 3 -> thirds and 1 -> all-out-at-T1 — i.e. the book could not actually express "half at T1 plus a
# runner" on the very names it was added to trade. At $250k every ticker sizes >=19 contracts, so the
# 1/2-1/4-1/4 shape is always executable and T2/T3 finally get TESTED rather than merely declared.
# NOTE the concurrent-exposure math changed with the universe: 10 tickers x 1% = up to 10% of the book
# at risk simultaneously (it was 2% with two tickers). Same per-trade discipline, 5x the breadth.
START_EQUITY = 250_000.0     # owner-funded paper book
# AUTO-REFILL. Owner's call: keep the scalp book funded so position sizing stays meaningful over the
# month-long study — a shrinking book quietly shrinks every trade and muddies the per-ticker data. When
# equity drops near REFILL_TRIGGER, inject REFILL_AMOUNT. Tracked SEPARATELY (injected capital) so the
# headline stays honest: TRUE P&L = equity - START - injected, never inflated by the top-ups.
REFILL_TRIGGER = 150_000.0
REFILL_AMOUNT = 100_000.0
RISK_PCT = 1.0               # % of CURRENT equity risked per position
MAX_CONCURRENT = 4           # open positions at once — a real book has finite attention and margin
RT_COST_PER_CT = 1.30        # $1.00 open + ~$0.30 fees; $0 to close
BOOK_FILE = _P.CHANNELS / "chip" / "paper_book.json"
# MEASUREMENT START — the book (and the R measurement) begins here. Everything BEFORE this date was
# recorded before the 20-min time-stop scoring fix and is not a clean read (e.g. the QQQ 708C held
# 12.6h and booked -1.13R). Owner's call: exclude 07-22 from the $50k roll, start flat at 07-23. The
# raw rows stay in the DB (nothing is deleted) — they are simply not counted in the book's equity.
MEASUREMENT_START = "2026-07-23"      # UTC date; trades closing before this are excluded from the roll


def size_position(equity, entry_px, stop_pct, risk_pct=RISK_PCT):
    """Contracts, the $ risked, and WHY. Returns (qty, risk_per_contract, note)."""
    if not entry_px or not stop_pct or entry_px <= 0:
        return 0, None, "no price"
    risk_ct = stop_pct / 100.0 * entry_px * 100.0
    budget = equity * (risk_pct / 100.0)
    qty = int(budget // risk_ct)
    if qty < 1:
        return 0, round(risk_ct, 2), (f"${risk_ct:,.0f}/contract exceeds the ${budget:,.0f} budget "
                                      f"({risk_pct}% of ${equity:,.0f}) — too big for this book")
    return qty, round(risk_ct, 2), f"{qty} ct x ${risk_ct:,.0f} risk = ${qty*risk_ct:,.0f} ({risk_pct}%)"


def _trades(con):
    """Every RESOLVED chip trade with the fields needed for contract-level accounting, oldest first.
    return_pct is the desk's cost-honest number (ask-in / bid-out / commission), so scaling it by
    contracts is legitimate — the per-contract commission is already inside the percentage."""
    return con.execute(
        "SELECT o.signal_id, s.ticker, s.strike, s.type, s.expiry, s.stop_pct, s.t1_pct, "
        "       o.entry_px, o.return_pct, o.r_multiple, o.result, o.opened_utc, o.closed_utc, "
        "       o.peak_pct, o.trough_pct, o.hold_secs "
        "FROM outcomes o JOIN signals s ON s.signal_id = o.signal_id "
        "WHERE s.provider='chip' AND o.kind='SHADOW' AND o.return_pct IS NOT NULL "
        # Measurement starts at MEASUREMENT_START — a trade that CLOSED before it is excluded from the
        # roll (kept in the DB, just not counted). closed_utc is the day the book realises the P&L.
        "AND COALESCE(o.closed_utc, o.opened_utc) >= ? "
        "ORDER BY COALESCE(o.opened_utc, o.signal_id)", (MEASUREMENT_START,)).fetchall()


def replay(con=None):
    """Rebuild the whole book from resolved trades. DERIVED, never incrementally mutated — so it is
    reproducible from the DB, cannot drift, and a re-score of the underlying flows straight through."""
    own = con is None
    if own:
        import db as DB
        con = sqlite3.connect(str(DB.DB_PATH))
    try:
        return _summarise(*_build_rows(con))
    finally:
        if own:
            try: con.close()
            except Exception: pass


def trades_all(con=None):
    """EVERY resolved chip trade, uncapped — the same rows replay() builds before its 60-row display cap.
    Backs the per-ticker edge box, which needs per-trade win/loss, not just the recent window."""
    own = con is None
    if own:
        import db as DB
        con = sqlite3.connect(str(DB.DB_PATH))
    try:
        return _build_rows(con)[0]
    finally:
        if own:
            try: con.close()
            except Exception: pass


def _build_rows(con):
    """The core replay loop, factored out so replay() and trades_all() share ONE source of truth."""
    if True:
        eq = peak = START_EQUITY
        injected = 0.0
        injections = []
        rows, refused, plans = [], [], []
        daily = defaultdict(float)
        for (sid, tk, strike, typ, exp, stop_pct, t1_pct, entry, ret, r, result,
             opened, closed, peak_pct, trough_pct, hold) in _trades(con):
            entry = float(entry or 0)
            stop_pct = float(stop_pct or 30)
            qty, risk_ct, note = size_position(eq, entry, stop_pct)
            day = (closed or opened or "")[:10]
            if qty < 1:
                refused.append({"signal_id": sid, "leg": f"{tk} {strike:.0f}{typ}", "why": note,
                                "day": day})
                continue
            # $ P&L = cost-honest return% applied to the notional actually held
            pnl = (float(ret) / 100.0) * entry * 100.0 * qty
            eq += pnl
            daily[day] += pnl
            # AUTO-REFILL: top up the moment the book dips to the trigger, so the NEXT trade sizes off a
            # healthy balance. Can fire more than once if a big loss blows through a whole tranche.
            while eq <= REFILL_TRIGGER:
                eq += REFILL_AMOUNT
                injected += REFILL_AMOUNT
                injections.append({"day": day, "amount": REFILL_AMOUNT, "equity_after": round(eq, 2)})
            peak = max(peak, eq)
            # THE EXECUTABLE PLAN, not the intended one. 1/2-1/4-1/4 only divides at multiples of 4, and
            # this book sizes to 3 and 7 — so "half out at T1" meant selling 1.5 and 3.5 contracts, orders
            # that cannot be placed. Recording the plan we could actually TRADE is what makes the P&L and
            # the T2/T3 coverage honest; scoring an unexecutable ladder measures a strategy nobody can run.
            import exec_plan as EP
            plan = EP.resolve_ladder(qty, t1_pct or 30, (t1_pct or 30) * 2, (t1_pct or 30) * 3, stop_pct)
            tests = EP.tests_t2_t3(plan)
            plans.append(plan)
            rows.append({
                "exec_plan": plan["note"], "exec_shape": plan["shape"],
                "ladder_degraded": plan["degraded"], "tests": tests,
                "signal_id": sid, "day": day, "opened": opened, "closed": closed,
                "leg": f"{tk} {strike:.0f}{typ}", "ticker": tk, "expiry": exp,
                "qty": qty, "entry": round(entry, 2),
                "risk_usd": round(qty * (risk_ct or 0), 2), "sizing": note,
                "return_pct": round(float(ret), 2), "r": round(float(r or 0), 3),
                "pnl": round(pnl, 2), "equity_after": round(eq, 2),
                "result": result, "peak_pct": peak_pct, "trough_pct": trough_pct,
                # Fall back to opened->closed when hold_secs is absent, so old rows recorded before the
                # hold_secs fix still show a real duration instead of 0m.
                "hold_min": round((hold if hold else _secs_between(opened, closed) or 0) / 60.0, 1),
            })
            # POST-CLOSE checklist: what we learn once it is done — realised dollars on the contract
            # count actually held, exit vs peak, whether the plan that RAN was the plan intended, and
            # what the round trip cost against how much the trade actually moved.
            try:
                import trade_checks as TC
                _pc = TC.post_trade(row=rows[-1], life={"end_reason": result})
                rows[-1]["checks"] = {"stage": "post", "items": _pc, "summary": TC.summarise(_pc)}
            except Exception:
                pass
        return rows, refused, daily, eq, peak, plans, injected, injections


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


def _cells(rows):
    """Uncapped {'day|ticker': pnl} over ALL rows — the pairing's source of truth, immune to the
    display cap on `trades`."""
    agg = defaultdict(float)
    for r in rows:
        agg[f"{r['day']}|{r['ticker']}"] += r["pnl"]
    return {k: round(v, 2) for k, v in agg.items()}


def _summarise(rows, refused, daily, eq, peak, plans=None, injected=0.0, injections=None):
    n = len(rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    gross_w = sum(r["pnl"] for r in wins)
    gross_l = sum(r["pnl"] for r in losses)
    # TRUE cumulative trading P&L = book equity minus the starting stake minus every top-up. The refills
    # keep the book funded for sizing but must NEVER flatter the performance number — this is the real
    # money made/lost by the strategy, and it is what `pnl` reports.
    injected = injected or 0.0
    true_pnl = eq - START_EQUITY - injected
    deployed = START_EQUITY + injected
    by = {}
    for kind in ("day", "week", "month"):
        agg = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})
        for r in rows:
            k = _period(r["day"], kind)
            agg[k]["pnl"] += r["pnl"]; agg[k]["n"] += 1
            if r["pnl"] > 0: agg[k]["wins"] += 1
        by[kind] = [{"period": k, "pnl": round(v["pnl"], 2), "n": v["n"], "wins": v["wins"],
                     "win_rate": round(100.0 * v["wins"] / v["n"], 1) if v["n"] else None}
                    for k, v in sorted(agg.items())]
    return {
        "start": START_EQUITY, "equity": round(eq, 2), "peak": round(peak, 2),
        # pnl = TRUE trading P&L (excludes the top-ups). equity = the funded book balance chip sizes off.
        "pnl": round(true_pnl, 2),
        "injected": round(injected, 2), "refills": len(injections or []),
        "injections": (injections or [])[-20:], "deployed": round(deployed, 2),
        "return_pct": round((true_pnl / deployed) * 100, 2) if deployed else 0.0,
        "drawdown_pct": round((eq / peak - 1) * 100, 2) if peak else 0.0,
        "risk_pct": RISK_PCT, "risk_per_trade": round(eq * RISK_PCT / 100.0, 2),
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / n, 1) if n else None,
        "avg_win": round(gross_w / len(wins), 2) if wins else None,
        "avg_loss": round(gross_l / len(losses), 2) if losses else None,
        "profit_factor": round(gross_w / abs(gross_l), 2) if gross_l else None,
        "expectancy_usd": round(true_pnl / n, 2) if n else None,
        "by_day": by["day"], "by_week": by["week"], "by_month": by["month"],
        "trades": rows[-60:],
        # UNCAPPED per-(day,ticker) P&L for the theta pairing. `trades` above is capped at 60 for the
        # payload, and paired_vs_chip used to consume THAT — so on a 180-trade day it silently paired
        # only the last 60 and reported the buyer at -11,948 when the book's own by_day said -49,863.
        # Same cap-mistaken-for-full-data bug as the scalp log. This field aggregates ALL rows, and JSON
        # cannot key on a tuple so the key is "day|ticker".
        "cells": _cells(rows),
        "refused": refused[-20:], "n_refused": len(refused),
        # DOES THE BOOK ACTUALLY TEST ITS OWN TARGETS? A book of 1-lots can never learn whether its T2/T3
        # are any good, because the data to answer that is never generated. This is that number.
        "ladder_coverage": (__import__("exec_plan").coverage(plans) if plans else {"n": 0}),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "note": ("shadow-only paper book. Costs are real: ask-in, bid-out, $1.30/contract. "
                 "chip's measured entry edge is not distinguishable from random, so the honest "
                 "expectation for this book is a slow bleed — it is an instrument, not a bankroll."),
    }


def equity(con=None):
    try:
        return replay(con)["equity"]
    except Exception:
        return START_EQUITY


def save(state=None):
    try:
        st = state or replay()
        BOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOOK_FILE.write_text(json.dumps(st, indent=1))
        return st
    except Exception:
        return None


if __name__ == "__main__":
    s = replay()
    print(f"chip paper book  ${s['equity']:,.2f}   ({s['pnl']:+,.2f}, {s['return_pct']:+.2f}%)")
    print(f"  {s['n']} trades  {s['wins']}W/{s['losses']}L  win {s['win_rate']}%  "
          f"avg win ${s['avg_win']}  avg loss ${s['avg_loss']}  PF {s['profit_factor']}")
    print(f"  risk/trade ${s['risk_per_trade']:,.2f}  peak ${s['peak']:,.2f}  dd {s['drawdown_pct']}%")
    if s["n_refused"]:
        print(f"  {s['n_refused']} refused on size")
    for k in ("by_day", "by_week", "by_month"):
        if s[k]:
            print(f"  {k}:")
            for p in s[k][-8:]:
                print(f"     {p['period']}  {p['pnl']:+9,.2f}  n={p['n']}  win {p['win_rate']}%")
