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
# STOP-LOSS: OFF until evidence justifies it. None = no stop (defined-risk wing + EOD flatten only, the
# documented SME default). Set to e.g. 2.5 to close when the loss reaches 2.5x the credit collected.
# Decide the number from seller_stop_lab (replays real trades against the ladder), not by guessing —
# and note there are ZERO losing trades so far, so a stop would not yet have fired on anything.
SELLER_STOP_MULT = None
# Within this many minutes of the 16:00 ET close, FORCE-flatten every open position (settling at
# intrinsic if quotes are gone). 3 min, because the ladder's last snapshot lands ~2 min before the bell.
EOD_SETTLE_MIN = 3


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
    snapshot for `ticker`. Returns (chain, spot, ts, expiry) or ([], None, None, None)."""
    try:
        import ladder_logger as LL
        con = sqlite3.connect("file:" + str(LL.LADDER_DB) + "?mode=ro", uri=True)
    except Exception:
        return [], None, None, None
    try:
        ts = con.execute("SELECT MAX(ts_utc) FROM ladder WHERE ticker=?", (ticker,)).fetchone()
        if not ts or not ts[0]:
            return [], None, None, None
        ts = ts[0]
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
        if age > FRESH_SEC:
            return [], None, None, None
        rows = con.execute(
            "SELECT strike, right, bid, ask, mid, iv, delta, spot, expiry FROM ladder "
            "WHERE ticker=? AND ts_utc=?", (ticker, ts)).fetchall()
        chain = [{"strike": r[0], "right": r[1], "bid": r[2], "ask": r[3], "mid": r[4],
                  "iv": r[5], "delta": r[6]} for r in rows]
        spot = rows[0][7] if rows else None
        expiry = rows[0][8] if rows else None      # which expiry these strikes belong to (YYYYMMDD)
        return chain, spot, ts, expiry
    finally:
        con.close()


def _expiry_day(p):
    """The position's expiry as YYYY-MM-DD. An expired option settles at ITS expiry-day close, so this is
    the day whose spot we must settle against — not 'today'. Falls back to the trade day (0DTE)."""
    e = str(p.get("expiry") or "")
    if len(e) >= 8:
        return f"{e[:4]}-{e[4:6]}-{e[6:8]}"
    return p.get("day")


def _last_spot(ticker, on_day=None):
    """Ladder spot for a ticker. With on_day (YYYY-MM-DD) it returns that DAY'S last spot — which is the
    correct settlement price for an option that expired that day, and is robust to the desk being DOWN
    across the close: settling a Friday 0DTE on Monday must use Friday's close, not Monday's spot. No
    freshness gate; the chain is cold by the time this is needed."""
    try:
        import ladder_logger as LL
        con = sqlite3.connect("file:" + str(LL.LADDER_DB) + "?mode=ro", uri=True)
    except Exception:
        return None
    try:
        if on_day:
            r = con.execute("SELECT spot FROM ladder WHERE ticker=? AND day=? ORDER BY ts_utc DESC LIMIT 1",
                            (ticker, on_day)).fetchone()
            if r:
                return r[0]
        r = con.execute("SELECT spot FROM ladder WHERE ticker=? ORDER BY ts_utc DESC LIMIT 1",
                        (ticker,)).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def _intrinsic_buyback(spot, p):
    """Cost to SETTLE the spread at expiry given the final spot — intrinsic value, and deliberately NOT
    quote-based. The quote mark returns None the moment a wing goes worthless (no bid), which is exactly
    when a 0DTE position is deep in trouble — so the quote path left 4 breached condors hanging open,
    unbooked, and the next-day fallback then assumed them expired WORTHLESS (full credit kept) when they
    were losses. Intrinsic is the true expiry value and needs no quote: call = clamp(S-Ks, 0, width),
    put = clamp(Ks-S, 0, width); a condor settles both."""
    sp = p.get("spread") or {}
    if spot is None:
        return None

    def _vert(short_k, long_k, right):
        w = abs(long_k - short_k)
        return max(0.0, min(spot - short_k, w)) if right == "C" else max(0.0, min(short_k - spot, w))

    if p.get("kind") == "condor":
        c, pu = sp.get("call") or {}, sp.get("put") or {}
        if c.get("short_strike") is None or pu.get("short_strike") is None:
            return None
        return round(_vert(c["short_strike"], c["long_strike"], "C")
                     + _vert(pu["short_strike"], pu["long_strike"], "P"), 3)
    if sp.get("short_strike") is None:
        return None
    return round(_vert(sp["short_strike"], sp["long_strike"], sp.get("right")), 3)


def _settle_all_orphans(open_rows, now):
    """Market is closed: every still-open position is an expired 0DTE that never got flattened intraday.
    Settle each at intrinsic vs the last spot the ladder holds for it, and book it. Returns [] — nothing
    survives past its own expiry. This is the backstop for the intraday EOD flatten (see _manage_open),
    and what recovers positions orphaned by a worthless-wing None-quote or a restart across the close."""
    now_iso = now.astimezone(timezone.utc).isoformat()
    for p in open_rows:
        spot = _last_spot(p["ticker"], _expiry_day(p))       # the EXPIRY DAY'S close, not today's spot
        bb = _intrinsic_buyback(spot, p)
        _resolve(p, bb, "expiry_settle", now_iso, close_legs=[{"settle": "intrinsic", "spot": spot}])
    return []


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
    """Current BUYBACK of a vertical from the chain: buy back short (its ask), sell long (its bid).
    Returns {buyback, short_ask, long_bid, ...} — per-leg close prices, not just the net, so the resolved
    record can show 'bought the short back at X, sold the wing at Y', mirroring the entry legs."""
    s = next((c for c in chain if (c["right"] or "").upper() == right and abs(c["strike"] - short_strike) < 1e-6), None)
    l = next((c for c in chain if (c["right"] or "").upper() == right and abs(c["strike"] - long_strike) < 1e-6), None)
    if not s or not l:
        return None
    return {"buyback": SE.spread_buyback(s.get("ask"), l.get("bid")),
            "right": right, "short_strike": short_strike, "long_strike": long_strike,
            "short_ask": s.get("ask"), "long_bid": l.get("bid")}


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
        chain, spot, ts, expiry = _latest_chain(tk)
        if not chain:
            continue
        # ARM 1 — directional on chip's side
        if (tk, "arm1") not in fired:
            side = _chip_side(tk)
            v = SE.directional(chain, side) if side else None
            if v and _tradeable(tk, "arm1", v):
                q, risk, note = SE.size(equity, v["defined_risk"])
                if q >= 1:
                    opened.append(_new_pos("arm1", tk, v, q, risk, note, spot, now_iso, day, expiry))
                    fired.add((tk, "arm1"))
        # ARM 2 — neutral condor
        if (tk, "arm2") not in fired:
            c = SE.build_condor(chain)
            if c and _tradeable(tk, "arm2", c):
                q, risk, note = SE.size(equity, c["defined_risk"])
                if q >= 1:
                    opened.append(_new_pos("arm2", tk, c, q, risk, note, spot, now_iso, day, expiry))
                    fired.add((tk, "arm2"))
    return opened


def _new_pos(arm, tk, spread, qty, risk_usd, note, spot, now_iso, day, expiry=None):
    legs = spread.get("legs", 2)
    return {"arm": arm, "ticker": tk, "kind": spread.get("kind", "vertical"), "spread": spread,
            "qty": qty, "risk_usd": risk_usd, "sizing": note, "entry_spot": spot,
            "expiry": expiry,        # the contract expiry these legs trade (YYYYMMDD) — 0DTE if == day
            "credit": spread["credit"], "defined_risk": spread["defined_risk"], "legs": legs,
            "open_comm": SE.open_cost(legs) * qty, "opened": now_iso, "day": day, "state": "open"}


def _manage_open(open_rows, now, day, session_min):
    """Mark & manage every open position; move resolved ones to seller_book. Returns the still-open list."""
    still = []
    mins_to_close = max(0, 390 - session_min)
    now_iso = now.astimezone(timezone.utc).isoformat()
    for p in open_rows:
        # PRIOR-DAY ORPHAN — a 0DTE that never settled. Book it at INTRINSIC vs the last spot, not the
        # old "stale_eod, buyback=None" which kept full credit and turned breached losers into wins.
        if p.get("day") != day:
            spot = _last_spot(p["ticker"], _expiry_day(p))    # settle at the expiry day's close
            _resolve(p, _intrinsic_buyback(spot, p), "expiry_settle_late", now_iso,
                     close_legs=[{"settle": "intrinsic", "spot": spot}])
            continue
        chain, spot, ts, _exp = _latest_chain(p["ticker"])
        sp = p["spread"]
        buyback, close_legs = None, None
        if chain:
            if p["kind"] == "condor":
                m_c = _mark_vertical(chain, "C", sp["call"]["short_strike"], sp["call"]["long_strike"])
                m_p = _mark_vertical(chain, "P", sp["put"]["short_strike"], sp["put"]["long_strike"])
                both = m_c and m_p and m_c["buyback"] is not None and m_p["buyback"] is not None
                buyback = (m_c["buyback"] + m_p["buyback"]) if both else None
                close_legs = [m_c, m_p] if (m_c and m_p) else None
            else:
                m = _mark_vertical(chain, sp["right"], sp["short_strike"], sp["long_strike"])
                buyback = m["buyback"] if m else None
                close_legs = [m] if m else None
        # EOD FLATTEN — force a close in the final minutes, and settle at INTRINSIC when the quote mark
        # is unusable (a worthless wing has no bid -> buyback None). This is the exact gap that left 4
        # breached condors open past the close: the quote mark was None, so manage() just held them, then
        # the loop stopped at 16:00 and they were orphaned. Never rely on a quote to close a 0DTE.
        if mins_to_close <= EOD_SETTLE_MIN:
            s = spot if spot is not None else _last_spot(p["ticker"])
            bb = buyback if buyback is not None else _intrinsic_buyback(s, p)
            _resolve(p, bb, "eod", now_iso,
                     close_legs=close_legs or [{"settle": "intrinsic", "spot": s}])
            continue
        if not chain:
            still.append(p); continue
        action, pnl_per_ct, reason = SE.manage(p["credit"], buyback, mins_to_close, p["legs"],
                                               stop_mult=SELLER_STOP_MULT)
        if action in ("take_profit", "eod", "stop"):
            _resolve(p, buyback, action, now.astimezone(timezone.utc).isoformat(), pnl_per_ct, close_legs)
        else:
            p["mark_buyback"], p["open_pnl"] = buyback, round((pnl_per_ct or 0) * p["qty"], 2)
            still.append(p)
    return still


def _resolve(p, buyback, trigger, now_iso, pnl_per_ct=None, close_legs=None):
    if pnl_per_ct is None:
        rt = SE.COMM_PER_LEG * 2 * p["legs"]
        pnl_per_ct = round(((p["credit"] - (buyback if buyback is not None else 0)) * 100) - rt, 2)
    SB.record({**{k: p.get(k) for k in ("arm", "ticker", "kind", "credit", "defined_risk", "qty",
                                         "entry_spot", "opened", "day", "expiry")},
               # KEEP THE STRUCTURE. The resolved record dropped `spread`, so the ledger could show P&L
               # but never WHICH spread produced it — the click-to-expand detail (strikes, wings, short
               # delta/IV, the friction that gated it) had nothing to render for a closed trade.
               "spread": p.get("spread"), "legs": p.get("legs"),
               # PER-LEG CLOSE FILLS to sit beside the entry legs in the spread: what we paid to buy the
               # short back and what we sold the wing for. With these the record is complete end to end —
               # every leg, both prices.
               "close_legs": close_legs,
               "closed": now_iso, "buyback": buyback, "mgmt_trigger": trigger,
               "pnl": round(pnl_per_ct * p["qty"], 2)})


def scan_loop():
    print("\U0001f4b0 seller desk: started (premium-seller books arm1+arm2, shadow paper).", flush=True)
    while True:
        try:
            now = datetime.now(ET)
            smin = (now.hour * 60 + now.minute) - (9 * 60 + 30)
            if now.weekday() >= 5 or not (0 <= smin <= 390):
                # MARKET CLOSED — but positions must never hang open past their 0DTE expiry. Settle any
                # still-open ones at intrinsic (this is what recovers a position orphaned by a worthless
                # wing or a restart across the close). No-op once the book is flat.
                orphans = _load_open()
                if orphans:
                    _save_open(_settle_all_orphans(orphans, now))
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
