#!/usr/bin/env python3
"""
seller_desk.py — the live driver for the premium-SELLER books (long theta), the mirror of chip_desk.
Runs as a daemon on the desk. Once the morning settles (~10:00 ET) it opens, per ticker:
  · ARM 1 — a directional credit spread on CHIP'S CURRENT EMA SIDE (isolates the theta sign)
  · ARM 2 — a neutral 0DTE iron condor (pure short-vol)
then marks and manages them every lap (50% profit-take / EOD flatten; the wing is the stop, no 2x),
recording each resolved position into seller_book. SHADOW-ONLY paper — never touches a broker.

Reuses the ladder logger's data as its chain (channels/ladder.db): the ladder already streams the OTM
strikes+greeks every minute, so the seller desk needs NO extra IBKR lines — it reads the latest snapshot,
builds spreads with seller_engine, and marks open positions off subsequent snapshots. If the ladder has
no fresh snapshot yet (pre-open / cold), it simply waits — fail-soft, never throws.
"""
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import deskpaths as _P
import seller_engine as SE
import seller_book as SB

ET = timezone(timedelta(hours=-4))
# FULL UNIVERSE, matching chip and the ladder. Owner's call, asked twice. The original SPY/QQQ-only
# rule was sound in substance — you do not sell defined-risk spreads into a 10% quote — but it enforced
# that by never LOOKING at the other names, which also meant never learning whether they were sellable.
# The rule now lives where it belongs: a per-trade exit-friction gate (_tradeable), evaluated on the
# day's real quote. Wide days get refused and logged; tight days get traded.
TICKERS = ("SPY", "QQQ", "IWM", "NVDA", "AAPL", "GOOGL", "TSLA", "META", "AMZN", "MSFT")
OPEN_STORE = _P.CHANNELS / "seller" / "open.json"      # live (unresolved) positions
ENTRY_AFTER_MIN = 30                                    # open ~10:00 ET (30 min after the 9:30 open)
# A ladder snapshot older than this is stale. MUST exceed the logger's LAP time, not just its nominal
# 60s sleep: the lap is sequential over the universe, so widening from 2 tickers to 10 stretched it from
# ~60s to ~270s measured. With the old flat 180 every chain read stale the moment the universe grew and
# the seller silently found "no chain" for ALL TEN tickers — including the two that had always worked.
# Scaled off the universe so this cannot silently break again the next time a ticker is added.
FRESH_SEC = max(180, 60 * len(TICKERS))
_FIRED = {}                                             # day -> set of (ticker, arm) already opened


def _load_open():
    try:
        d = json.loads(OPEN_STORE.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_open(rows):
    try:
        OPEN_STORE.parent.mkdir(parents=True, exist_ok=True)
        from safe_io import safe_write_json
        safe_write_json(OPEN_STORE, rows, min_keep_ratio=0.0)
    except Exception:
        pass


def _latest_chain(ticker):
    """Build a chain [{strike,right,delta,bid,ask,mid,iv}] from the ladder logger's most recent fresh
    snapshot for `ticker`. Returns (chain, spot, ts) or ([], None, None)."""
    try:
        import ladder_logger as LL
        con = sqlite3.connect("file:" + str(LL.LADDER_DB) + "?mode=ro", uri=True)
    except Exception:
        return [], None, None
    try:
        ts = con.execute("SELECT MAX(ts_utc) FROM ladder WHERE ticker=?", (ticker,)).fetchone()
        if not ts or not ts[0]:
            return [], None, None
        ts = ts[0]
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
        if age > FRESH_SEC:
            return [], None, None
        rows = con.execute(
            "SELECT strike, right, bid, ask, mid, iv, delta, spot FROM ladder "
            "WHERE ticker=? AND ts_utc=?", (ticker, ts)).fetchall()
        chain = [{"strike": r[0], "right": r[1], "bid": r[2], "ask": r[3], "mid": r[4],
                  "iv": r[5], "delta": r[6]} for r in rows]
        spot = rows[0][7] if rows else None
        return chain, spot, ts
    finally:
        con.close()


def _chip_side(ticker):
    """chip's current EMA/tape side for the ticker ('C' bullish / 'P' bearish), for Arm 1's direction."""
    try:
        import chip_desk as CD
        for k in (f"{ticker}|C", f"{ticker}|P"):
            st = CD.STATUS.get(k)
            if st and st.get("primary"):
                return st.get("side")
        st = CD.STATUS.get(f"{ticker}|C") or CD.STATUS.get(f"{ticker}|P")
        return st.get("side") if st else None
    except Exception:
        return None


def _mark_vertical(chain, right, short_strike, long_strike):
    """Current BUYBACK cost of a vertical from the chain: buy back short (ask), sell long (bid)."""
    s = next((c for c in chain if (c["right"] or "").upper() == right and abs(c["strike"] - short_strike) < 1e-6), None)
    l = next((c for c in chain if (c["right"] or "").upper() == right and abs(c["strike"] - long_strike) < 1e-6), None)
    if not s or not l:
        return None
    return SE.spread_buyback(s.get("ask"), l.get("bid"))


def _already_open_today(open_rows, day):
    """(ticker, arm) pairs that ALREADY have a position today — from live open rows AND from today's
    resolved positions in the book.

    RESTART-PROOF, and it has to be: the old in-memory _FIRED set reset on every process restart, so
    three restarts this morning made the desk re-open the same spreads three times (8 positions where
    there should have been 4). chip_desk solves the same problem with _rebuild_book; this is that."""
    seen = {(p.get("ticker"), p.get("arm")) for p in open_rows if p.get("day") == day}
    try:
        for p in SB._load():
            if (p.get("day") or (p.get("closed") or "")[:10]) == day:
                seen.add((p.get("ticker"), p.get("arm")))
    except Exception:
        pass
    return seen


REFUSALS = []          # (day, ticker, arm, ratio) — why a name was skipped; bounded, surfaced on /api


def _friction_of(spread):
    """Worst leg-pair friction in the structure. A condor is two verticals; the WORSE side governs,
    because you have to be able to close both."""
    if spread.get("kind") == "condor":
        parts = [spread.get("call") or {}, spread.get("put") or {}]
        ratios = [p.get("friction_ratio") for p in parts if p.get("friction_ratio") is not None]
        return max(ratios) if ratios else None
    return spread.get("friction_ratio")


def _tradeable(tk, arm, spread):
    """Is this spread's quote good enough to actually trade? See seller_engine.MAX_FRICTION_RATIO —
    the round-trip exit cost must not eat the credit. This is what makes it safe to run the seller
    across the whole universe instead of hard-coding it to SPY/QQQ: a wide name is refused on the day's
    ACTUAL quote, and refusals are recorded rather than silently dropped, so the threshold can later be
    replaced with evidence instead of judgement."""
    fr = _friction_of(spread)
    if fr is None:
        return True                      # no quote detail — fall through to the existing credit checks
    if fr > SE.MAX_FRICTION_RATIO:
        REFUSALS.append({"ticker": tk, "arm": arm, "friction_ratio": fr,
                         "credit": spread.get("credit"), "why": "exit friction > cap"})
        del REFUSALS[:-60]
        return False
    return True


def _open_positions(equity, day, now_iso, open_rows=()):
    # derive from STATE, not an in-memory flag, so a restart can't duplicate the day's entries
    fired = _FIRED.setdefault(day, set()) | _already_open_today(open_rows, day)
    opened = []
    for tk in TICKERS:
        chain, spot, ts = _latest_chain(tk)
        if not chain:
            continue
        # ARM 1 — directional on chip's side
        if (tk, "arm1") not in fired:
            side = _chip_side(tk)
            v = SE.directional(chain, side) if side else None
            if v and _tradeable(tk, "arm1", v):
                q, risk, note = SE.size(equity, v["defined_risk"])
                if q >= 1:
                    opened.append(_new_pos("arm1", tk, v, q, risk, note, spot, now_iso, day))
                    fired.add((tk, "arm1"))
        # ARM 2 — neutral condor
        if (tk, "arm2") not in fired:
            c = SE.build_condor(chain)
            if c and _tradeable(tk, "arm2", c):
                q, risk, note = SE.size(equity, c["defined_risk"])
                if q >= 1:
                    opened.append(_new_pos("arm2", tk, c, q, risk, note, spot, now_iso, day))
                    fired.add((tk, "arm2"))
    return opened


def _new_pos(arm, tk, spread, qty, risk_usd, note, spot, now_iso, day):
    legs = spread.get("legs", 2)
    return {"arm": arm, "ticker": tk, "kind": spread.get("kind", "vertical"), "spread": spread,
            "qty": qty, "risk_usd": risk_usd, "sizing": note, "entry_spot": spot,
            "credit": spread["credit"], "defined_risk": spread["defined_risk"], "legs": legs,
            "open_comm": SE.open_cost(legs) * qty, "opened": now_iso, "day": day, "state": "open"}


def _manage_open(open_rows, now, day, session_min):
    """Mark & manage every open position; move resolved ones to seller_book. Returns the still-open list."""
    still = []
    mins_to_close = max(0, 390 - session_min)
    for p in open_rows:
        if p.get("day") != day:                                  # a stale prior-day position -> force EOD
            _resolve(p, buyback=None, trigger="stale_eod", now=now.astimezone(timezone.utc).isoformat())
            continue
        chain, spot, ts = _latest_chain(p["ticker"])
        if not chain:
            still.append(p); continue
        sp = p["spread"]
        if p["kind"] == "condor":
            cb_c = _mark_vertical(chain, "C", sp["call"]["short_strike"], sp["call"]["long_strike"])
            cb_p = _mark_vertical(chain, "P", sp["put"]["short_strike"], sp["put"]["long_strike"])
            buyback = (cb_c + cb_p) if (cb_c is not None and cb_p is not None) else None
        else:
            buyback = _mark_vertical(chain, sp["right"], sp["short_strike"], sp["long_strike"])
        action, pnl_per_ct, reason = SE.manage(p["credit"], buyback, mins_to_close, p["legs"])
        if action in ("take_profit", "eod"):
            _resolve(p, buyback, action, now.astimezone(timezone.utc).isoformat(), pnl_per_ct)
        else:
            p["mark_buyback"], p["open_pnl"] = buyback, round((pnl_per_ct or 0) * p["qty"], 2)
            still.append(p)
    return still


def _resolve(p, buyback, trigger, now_iso, pnl_per_ct=None):
    if pnl_per_ct is None:
        rt = SE.COMM_PER_LEG * 2 * p["legs"]
        pnl_per_ct = round(((p["credit"] - (buyback if buyback is not None else 0)) * 100) - rt, 2)
    SB.record({**{k: p[k] for k in ("arm", "ticker", "kind", "credit", "defined_risk", "qty",
                                     "entry_spot", "opened", "day")},
               # KEEP THE STRUCTURE. The resolved record dropped `spread`, so the ledger could show P&L
               # but never WHICH spread produced it — the click-to-expand detail (strikes, wings, short
               # delta/IV, the friction that gated it) had nothing to render for a closed trade.
               "spread": p.get("spread"), "legs": p.get("legs"),
               "closed": now_iso, "buyback": buyback, "mgmt_trigger": trigger,
               "pnl": round(pnl_per_ct * p["qty"], 2)})


def scan_loop():
    print("\U0001f4b0 seller desk: started (premium-seller books arm1+arm2, shadow paper).", flush=True)
    while True:
        try:
            now = datetime.now(ET)
            smin = (now.hour * 60 + now.minute) - (9 * 60 + 30)
            if now.weekday() >= 5 or not (0 <= smin <= 390):
                time.sleep(30); continue
            day = now.strftime("%Y-%m-%d")
            now_iso = now.astimezone(timezone.utc).isoformat()
            equity = SB.replay()["equity"]
            open_rows = _load_open()
            if smin >= ENTRY_AFTER_MIN:
                open_rows += _open_positions(equity, day, now_iso, open_rows)
            open_rows = _manage_open(open_rows, now, day, smin)
            _save_open(open_rows)
            time.sleep(30)
        except Exception:
            time.sleep(20)


if __name__ == "__main__":
    print("seller_desk — runs as a daemon in server.py; drives seller_engine + seller_book live at open")
