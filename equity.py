#!/usr/bin/env python3
"""
Compounding + capital rules for the strategy sleeve (SME spec).

  - Sleeve compounds on its OWN realized P&L (start e.g. $15k).
  - High-water-mark ratchet + geometric drawdown throttle (halt at -20%).
  - Speed cap: sizing base may rise at most +20% per 5 trading days.
  - Profit-withdrawal MILESTONES: flags when to bank principal; withdrawals are
    recorded to a ledger, lower tradable equity, and reset the HWM base.
  - $10k tradable floor (0DTE sizing math breaks below it).

Withdrawals are USER-recorded (the system flags, you move the money):
    python3 equity.py                 # show sleeve state
    python3 equity.py withdraw 3750   # record a bank/withdrawal
"""
import csv, re, json, sys
from pathlib import Path

ROOT = Path(__file__).parent
try:                                    # shared 1R basis — must match signal_engine's sizing stop
    from signal_engine import SIZING_STOP_FRAC as STOP_FRAC
except Exception:
    STOP_FRAC = 0.35
FLOOR = 10000.0
LEDGER = ROOT / "channels" / "_withdrawals.json"


def _risk_R(row):
    m = re.search(r"R=([\d.]+)", row.get("notes", "") or "")
    return float(m.group(1)) if m else (0.5 if row.get("lotto") == "1" else 1.0)


def _realized_events():
    """ACTUAL realized $ per fully-closed trade, from the permanent closed_trades.jsonl record
    (the realized field already bakes in the real contract count). Time-ordered."""
    out = []
    for cj in ROOT.glob("channels/*/closed_trades.jsonl"):
        try:
            for ln in cj.read_text().splitlines():
                if not ln.strip():
                    continue
                rec = json.loads(ln)
                out.append((rec.get("closed") or rec.get("opened") or "", float(rec.get("realized") or 0)))
        except Exception:
            continue
    return out


def _open_realized():
    """Partial profits already banked on still-OPEN positions (not yet in closed_trades.jsonl)."""
    try:
        ps = json.loads((ROOT / "channels" / "_positions.json").read_text())
        return sum(float(p.get("realized") or 0) for p in ps if p.get("state") not in ("CLOSED", "CUT"))
    except Exception:
        return 0.0


def load_withdrawals():
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return []


def add_withdrawal(amount, date=None):
    from datetime import datetime, timezone, timedelta
    d = date or datetime.now(timezone(timedelta(hours=-4))).date().isoformat()
    wl = load_withdrawals()
    wl.append({"date": d, "amount": round(float(amount), 2)})
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(wl, indent=2))
    return wl


def throttle_for(dd):
    if dd > -0.05:  return 1.00
    if dd > -0.10:  return 0.75
    if dd > -0.15:  return 0.50
    if dd > -0.20:  return 0.35
    return 0.00


def recommended_banked(peak):
    """Cumulative amount the SME milestones say should be banked by a given peak."""
    rec = 0.0
    if peak >= 22500: rec += 3750         # +50% -> bank $3.75k
    if peak >= 30000: rec += 7500         # 2x   -> bank $7.5k (cum $11.25k)
    if peak > 30000:  rec += 0.25 * (peak - 30000)   # ongoing: 25% of gains above 2x
    return round(rec, 2)


def sleeve_state(start):
    start = float(start)
    events = [(ts, "t", d) for ts, d in _realized_events()]           # actual realized $ per closed trade
    events += [(w["date"] + "T23:59", "w", w["amount"]) for w in load_withdrawals()]
    events.sort()

    eq = hwm = peak = start
    daily = {}
    for ts, typ, val in events:
        if typ == "t":
            eq += val               # book ACTUAL realized $ (contract count already in it)
            hwm = max(hwm, eq)
            peak = max(peak, eq)
        else:                       # withdrawal: remove capital, reset HWM to post-sweep
            eq = max(0.0, eq - val)
            hwm = eq
        daily[ts[:10]] = eq
    eq += _open_realized()          # + partial trims banked on still-open positions
    hwm = max(hwm, eq); peak = max(peak, eq)
    if daily:
        daily[max(daily)] = eq
    withdrawn = round(sum(w["amount"] for w in load_withdrawals()), 2)

    dd = (eq / hwm - 1) if hwm > 0 else 0.0
    thr = throttle_for(dd)

    # speed cap: sizing base <= 1.2x equity from 5 trading days ago
    dates = sorted(daily)
    base_cap = min(eq, 1.2 * daily[dates[-6]]) if len(dates) >= 6 else eq
    sizing_base = round(min(eq, base_cap), 2)

    # milestone: what still needs banking, respecting the $10k floor
    due = max(0.0, recommended_banked(peak) - withdrawn)
    due = round(min(due, max(0.0, eq - FLOOR)), 2)

    return {"equity": round(eq, 2), "sizing_base": sizing_base, "hwm": round(hwm, 2),
            "peak": round(peak, 2), "drawdown": round(dd * 100, 1), "throttle": thr,
            "halted": thr == 0.0, "withdrawn": withdrawn, "milestone_due": due,
            "speed_capped": sizing_base < round(eq, 2) - 1}


def sleeve_equity(start):
    return sleeve_state(start)["sizing_base"]      # size off the capped base


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "withdraw":
        add_withdrawal(sys.argv[2])
        print(f"Recorded withdrawal ${float(sys.argv[2]):,.0f}.")
    print(json.dumps(sleeve_state(float(15000)), indent=2))
