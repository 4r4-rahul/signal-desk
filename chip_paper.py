#!/usr/bin/env python3
"""
chip_paper.py — chip's OWN paper account and ledger.

WHY chip needs its own book rather than borrowing the real one:

  1. SIZING. chip was sizing against the live tastytrade sleeve (~$9.5k, 1% = ~$96). QQQ 1DTE ATM risks
     ~$112/contract, so chip refused 100% of QQQ for two days — a measurement outage caused entirely by
     an account it does not trade. A shadow provider's sample should not be shaped by the real account's
     balance.
  2. LEDGER. chip's P&L belongs in its own equity curve, not mixed into a sleeve that funds real trades.
  3. HONESTY. Keeping the books separate makes it impossible for chip's paper performance to be mistaken
     for, or to influence, real risk state (drawdown halt, day-stop, provider sizing).

WHAT THIS IS NOT: a promotion. At the time of writing chip's entries measure WORSE THAN RANDOM
(null -0.142R vs chip's TAKE -0.219R, z = -3.58). The account exists so chip can generate an UNBIASED
SAMPLE across both tickers, not because it has earned capital. size_mult stays pinned at 0.0 and chip
never reaches broker.py.

ROLL RULE: chip risks RISK_PCT of its CURRENT paper equity per trade, so the account compounds and
decays like a real one. Equity is derived — replayed from chip's stored shadow outcomes in trade order,
never incrementally mutated — so it is reproducible from the DB and cannot drift.
"""
import json, sqlite3
from datetime import datetime, timezone

import deskpaths as _P

START_EQUITY = 25_000.0     # chip's opening paper balance
RISK_PCT = 1.0              # % of CURRENT paper equity risked per trade (1R)
LEDGER = _P.CHANNELS / "chip" / "paper_ledger.json"


def _rows(con):
    """chip's resolved shadow outcomes in trade order. R is the cost-honest r_multiple (ask-in/bid-out/
    commission) — the same number provider_edge uses, so the ledger cannot disagree with the edge table."""
    return con.execute(
        "SELECT o.signal_id, o.r_multiple, o.return_pct, o.opened_utc, o.closed_utc, "
        "       s.ticker, s.strike, s.type, s.expiry "
        "FROM outcomes o JOIN signals s ON s.signal_id = o.signal_id "
        "WHERE s.provider = 'chip' AND o.kind = 'SHADOW' AND o.r_multiple IS NOT NULL "
        "ORDER BY COALESCE(o.closed_utc, o.opened_utc, o.signal_id)").fetchall()


def replay(con=None):
    """Rebuild the whole curve from stored outcomes. Derived, never mutated — so it is reproducible and
    a re-score of the underlying shadows flows straight through."""
    own = con is None
    if own:
        import db as DB
        con = sqlite3.connect(str(DB.DB_PATH))
    try:
        eq = START_EQUITY
        peak = eq
        trades, wins = [], 0
        for sid, r, ret, opened, closed, tk, strike, typ, exp in _rows(con):
            risk = eq * (RISK_PCT / 100.0)          # 1R sized off CURRENT equity -> compounding
            pnl = float(r) * risk
            eq += pnl
            peak = max(peak, eq)
            if pnl > 0:
                wins += 1
            trades.append({"signal_id": sid, "leg": f"{tk} {strike}{typ}", "expiry": exp,
                           "r": round(float(r), 3), "return_pct": ret,
                           "risk": round(risk, 2), "pnl": round(pnl, 2), "equity": round(eq, 2),
                           "opened": opened, "closed": closed})
        n = len(trades)
        return {"start": START_EQUITY, "equity": round(eq, 2), "peak": round(peak, 2),
                "pnl": round(eq - START_EQUITY, 2),
                "return_pct": round((eq / START_EQUITY - 1) * 100, 2),
                "drawdown_pct": round((eq / peak - 1) * 100, 2) if peak else 0.0,
                "n": n, "wins": wins, "losses": n - wins,
                "win_rate": round(100.0 * wins / n, 1) if n else None,
                "avg_r": round(sum(t["r"] for t in trades) / n, 3) if n else None,
                "risk_per_trade": round(eq * (RISK_PCT / 100.0), 2),
                "risk_pct": RISK_PCT, "paper": True,
                "trades": trades[-50:],                      # bounded for the payload; full curve is derivable
                "as_of": datetime.now(timezone.utc).isoformat()}
    finally:
        if own:
            try: con.close()
            except Exception: pass


def equity(con=None):
    """chip's CURRENT paper equity — what its position sizing must be computed against."""
    try:
        return replay(con)["equity"]
    except Exception:
        return START_EQUITY          # fail to the opening balance, never to the real account


def save(state=None):
    """Persist the ledger for the UI / audit. Derived data, so losing it costs nothing."""
    try:
        st = state or replay()
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(st, indent=1))
        return st
    except Exception:
        return None


if __name__ == "__main__":
    s = replay()
    print(f"chip paper account: ${s['equity']:,.2f}  (start ${s['start']:,.0f}, {s['pnl']:+,.2f}, "
          f"{s['return_pct']:+.2f}%)")
    print(f"  {s['n']} trades  {s['wins']}W/{s['losses']}L  win {s['win_rate']}%  avg {s['avg_r']}R")
    print(f"  peak ${s['peak']:,.2f}  drawdown {s['drawdown_pct']}%  risk/trade ${s['risk_per_trade']:,.2f}")
