#!/usr/bin/env python3
"""
ladder_logger.py — THE UNRECOVERABLE PIECE. Log the OTM option ladder every minute, all session,
whether or not chip is in a trade.

WHY THIS EXISTS (the SME's #1 blocker): chip logs only the single ATM contract it buys. But a premium
SELLER trades OTM strikes and SPREADS — different contracts, whose minute-by-minute bid/ask/greeks are
never recorded. IBKR does NOT serve historical intraday 0DTE quotes for arbitrary strikes, so every
session we don't log the ladder is a session on which the forward seller books (Arm 1/Arm 2) can NEVER
be backtested — only run forward, live. This logger is therefore the gate: it must run from the open,
or the data is gone forever.

WHAT IT CAPTURES, per ticker, per minute, for the 0DTE expiry:
  · the OTM ladder — calls above spot + puts below spot spanning ~8-40 delta (the strikes a seller sells
    and the wings they buy), each with bid/ask/mid/iv/delta/gamma/theta/vega
  · a clean underlying snapshot (spot + session OHLC) INDEPENDENT of chip activity, so realized move,
    which-side-breached, and realized-vs-expected are always reconstructable

It runs on its OWN IBKR connection (own line budget, can't starve chip/the monitor) and writes to its
OWN SQLite file (channels/ladder.db) so this high-volume experiment data never bloats the desk DB.
Fail-soft everywhere: a missing quote is skipped, never thrown. Retained forever (owner rule).
"""
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import deskpaths as _P

ET = timezone(timedelta(hours=-4))
LADDER_DB = _P.CHANNELS / "ladder.db"
TICKERS = ("SPY", "QQQ")
CLIENT_ID = 84                      # own line budget, distinct from monitor(80/81)/ustop/chip(82)
SNAP_EVERY_S = 60                   # one ladder snapshot per minute per ticker
# DELTA BAND — must span the seller's SHORT strike *and its WING*, or no spread can be constructed.
# Caught live at the open: with a 0.06 floor the SPY ladder stopped at 745C (0.065d), but a 16d short
# sits at 743C and its $5 wing at 748C (~0.02d) was never logged -> build_vertical found no long leg and
# EVERY condor/vertical came back unbuildable. The floor must reach past the wing, and the ceiling is
# raised through ATM so marking and ATM-IV reads are covered too.
DELTA_LO, DELTA_HI = 0.02, 0.55
MAX_PER_SIDE = 14                   # wide enough for short+wing; still bounded for the IBKR line budget


def _con():
    con = sqlite3.connect(str(LADDER_DB), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS ladder(
        ts_utc TEXT, day TEXT, ticker TEXT, expiry TEXT, spot REAL,
        strike REAL, right TEXT, bid REAL, ask REAL, mid REAL,
        iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS underlying(
        ts_utc TEXT, day TEXT, ticker TEXT, spot REAL,
        open REAL, high REAL, low REAL, prev_close REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ladder_day ON ladder(day, ticker)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_und_day ON underlying(day, ticker)")
    return con


def _snap_ticker(MK, conn, ticker, day, now_iso, ohlc):
    """One minute's OTM ladder + underlying row for one ticker. Returns (ladder_rows, und_row)."""
    tk = ticker.upper()
    exps = MK.list_expiries(conn, tk)
    exp = exps[0] if exps else None                 # nearest = today's 0DTE (or next listed)
    spot = MK.spot_quote(tk, ib=conn)
    if not exp or not spot:
        return [], None
    strikes = MK._chain_strikes(conn, tk, exp)
    if not strikes:
        return [], None
    # OTM only: calls ABOVE spot, puts BELOW spot; walk outward, keep those in the delta band, bounded.
    ups = sorted([k for k in strikes if k >= spot])[:MAX_PER_SIDE * 2]
    downs = sorted([k for k in strikes if k <= spot], reverse=True)[:MAX_PER_SIDE * 2]
    rows = []
    for side_strikes, right in ((ups, "C"), (downs, "P")):
        kept = 0
        for k in side_strikes:
            if kept >= MAX_PER_SIDE:
                break
            q = MK.option_quote(tk, str(int(k)) if abs(k - int(k)) < 1e-9 else f"{k:g}", right,
                                expiry=exp, ib=conn)
            if not q or q.get("mid") is None:
                continue
            d = q.get("delta")
            if d is not None and not (DELTA_LO <= abs(d) <= DELTA_HI):
                # past the far edge of the band on this side -> stop walking outward
                if abs(d) < DELTA_LO:
                    break
                continue                              # too close to ATM yet -> keep walking out
            rows.append((now_iso, day, tk, exp, spot, float(k), right,
                         q.get("bid"), q.get("ask"), q.get("mid"),
                         q.get("iv"), q.get("delta"), q.get("gamma"), q.get("theta"), q.get("vega")))
            kept += 1
    o = ohlc.setdefault(tk, {"open": spot, "high": spot, "low": spot})
    o["high"] = max(o["high"], spot)
    o["low"] = min(o["low"], spot)
    und = (now_iso, day, tk, spot, o["open"], o["high"], o["low"], o.get("prev_close"))
    return rows, und


def scan_loop():
    """Daemon: own IBKR connection, one ladder+underlying snapshot per ticker per minute during RTH."""
    import market as MK
    print("\U0001fa9c ladder logger: started (OTM ladder + underlying, own conn).", flush=True)
    conn = None
    ohlc = {}
    last_day = None
    while True:
        try:
            now = datetime.now(ET)
            smin = (now.hour * 60 + now.minute) - (9 * 60 + 30)
            if now.weekday() >= 5 or not (0 <= smin <= 390):    # closed -> idle, reset the day's OHLC
                if now.strftime("%Y-%m-%d") != last_day:
                    ohlc = {}
                time.sleep(30)
                continue
            day = now.strftime("%Y-%m-%d")
            if day != last_day:                                 # new session -> fresh OHLC anchor
                ohlc = {}
                last_day = day
            if conn is None:
                conn = MK.connect(client_id=CLIENT_ID)
            all_rows, und_rows = [], []
            for tk in TICKERS:
                try:
                    rows, und = _snap_ticker(MK, conn, tk, day, now.astimezone(timezone.utc).isoformat(), ohlc)
                    all_rows += rows
                    if und:
                        und_rows.append(und)
                except Exception:
                    continue
            if all_rows or und_rows:
                con = _con()
                try:
                    if all_rows:
                        con.executemany("INSERT INTO ladder VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", all_rows)
                    if und_rows:
                        con.executemany("INSERT INTO underlying VALUES (?,?,?,?,?,?,?,?)", und_rows)
                    con.commit()
                finally:
                    con.close()
            try: MK.reap_subs(conn)
            except Exception: pass
            time.sleep(SNAP_EVERY_S)
        except Exception:
            try: conn.disconnect()
            except Exception: pass
            conn = None
            time.sleep(15)


def stats():
    """Quick coverage read: rows + distinct days + latest snapshot, per table."""
    try:
        con = _con()
        lr = con.execute("SELECT COUNT(*), COUNT(DISTINCT day), MAX(ts_utc) FROM ladder").fetchone()
        ur = con.execute("SELECT COUNT(*), COUNT(DISTINCT day), MAX(ts_utc) FROM underlying").fetchone()
        con.close()
        return {"ladder_rows": lr[0], "ladder_days": lr[1], "ladder_latest": lr[2],
                "underlying_rows": ur[0], "underlying_days": ur[1], "underlying_latest": ur[2]}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import sys
    if "stats" in sys.argv:
        print(stats())
    else:
        print("ladder_logger — run inside server.py as a daemon thread; `python ladder_logger.py stats` for coverage")
        print("schema check:", _con() and "ladder.db tables ready")
