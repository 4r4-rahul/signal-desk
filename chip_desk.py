#!/usr/bin/env python3
"""
chip_desk.py — "chip": a SELF-GENERATING signal provider on the desk.

Chip scans a small slate of liquid tickers during market hours, and when a ticker's live tape qualifies
an ATM-option scalp, it EMITS a signal through the normal ingest() path with provider="chip". From there
the desk's existing machinery does everything: cards it, AUTO-SHADOWS it (_init_shadow), replays the
scale-out discipline (½@T1→BE, ¼@T2→trail, runner, −1R) as the option moves, and rolls the result into
db.provider_edge() — so chip's expectancy is measured apples-to-apples next to the human providers.

SHADOW-ONLY by construction: chip never calls the broker. It emits a card; the card is shadow-tracked.
Real money stays gated behind an explicit go-live decision (see the gate in the design). Goal: grow chip's
provider expectancy — win rate↑, avg win↑, loss rate↓, avg loss↓, blended_r↑.

Complexity: the scan lap is O(T) over T tickers — warm spot/option reads from a persistent stream (~0ms),
bars cached 60s (no per-lap historical pulls), all state bounded (≤T book entries, ≤T prev-ind snapshots).
Chip runs on its own IBKR connection (client_id=82) with its own subscription pool, reaped at cap 80.
"""
import os, time, json
from datetime import datetime
from zoneinfo import ZoneInfo

import deskpaths as _P
import market as MK

ET = ZoneInfo("America/New_York")

# Phase 1 proves the loop on the two tightest-spread, cleanest-mechanics names. Phase 2 adds the rest.
TICKERS = ["SPY", "QQQ"]

# Per-ticker mechanics. *_pct are OPTION-premium %-moves (what _shadow_scaleout consumes), scaled to each
# ticker's ATM leverage — index ETFs (tight) get the smallest targets, single names ride wider. These are
# the fail-open DEFAULTS; chip_mechanics.json (nightly-calibrated) overrides them when present.
DEFAULT_MECHANICS = {
    "SPY":  {"stop_pct": 26, "t1_pct": 20, "t2_pct": 42, "t3_pct": 68,  "spread_max": 6,  "strike_inc": 1.0, "regime_bias": "trend"},
    "QQQ":  {"stop_pct": 28, "t1_pct": 22, "t2_pct": 46, "t3_pct": 74,  "spread_max": 6,  "strike_inc": 1.0, "regime_bias": "trend"},
    "NVDA": {"stop_pct": 34, "t1_pct": 30, "t2_pct": 62, "t3_pct": 100, "spread_max": 10, "strike_inc": 2.5, "regime_bias": "momentum"},
    "TSLA": {"stop_pct": 38, "t1_pct": 34, "t2_pct": 70, "t3_pct": 115, "spread_max": 12, "strike_inc": 5.0, "regime_bias": "range"},
    "AAPL": {"stop_pct": 30, "t1_pct": 24, "t2_pct": 50, "t3_pct": 80,  "spread_max": 8,  "strike_inc": 2.5, "regime_bias": "trend"},
}

MIN_TAPE_MODERATE = 62      # chip's own conviction bar (matches the desk's moderate grade)
COOLDOWN_S = 900            # 15 min between chip entries on the same ticker (anti-churn)
MAX_HOLD_S = 3600           # force-release a book slot after 1h even if the shadow hasn't formally ended
LOOP_SLEEP = 5              # scan cadence — 5s is plenty for scalp entries; keeps the lap cheap

_MECH_CACHE = {"ts": 0.0, "data": None}
_NEWS_CACHE = {"ts": 0.0, "data": None}
_LAST_EMIT = {}             # ticker -> ts of last chip emit (cooldown)


# ─────────────────────────── session / time ───────────────────────────
def _now_et():
    return datetime.now(ET)


def _session_min(now):
    """Minutes since the 9:30 ET open, or None when the market is closed (weekend / outside 9:30–16:00)."""
    if now.weekday() >= 5:
        return None
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 30)
    return mins if 0 <= mins <= 390 else None


# ─────────────────────────── config files (fail-open) ───────────────────────────
def _load_mechanics():
    if time.time() - _MECH_CACHE["ts"] < 60 and _MECH_CACHE["data"] is not None:
        return _MECH_CACHE["data"]
    data = dict(DEFAULT_MECHANICS)
    try:
        raw = json.loads((_P.CHANNELS / "chip" / "chip_mechanics.json").read_text())
        for tkr, m in (raw.get("tickers") or {}).items():
            data[tkr] = {**DEFAULT_MECHANICS.get(tkr, {}), **m}
    except Exception:
        pass                                            # fail-open: hardcoded defaults
    _MECH_CACHE.update(ts=time.time(), data=data)
    return data


def _news(tkr):
    """Per-ticker news overlay {bias, conf, veto} + macro veto. Fail-open: missing/stale -> no veto.
    (Phase 3 wires the out-of-band refresh; the interface + fail-open behavior live here now.)"""
    if time.time() - _NEWS_CACHE["ts"] >= 60 or _NEWS_CACHE["data"] is None:
        try:
            _NEWS_CACHE["data"] = json.loads((_P.CHANNELS / "chip" / "news.json").read_text())
        except Exception:
            _NEWS_CACHE["data"] = {}
        _NEWS_CACHE["ts"] = time.time()
    d = _NEWS_CACHE["data"] or {}
    fresh = True
    try:                                                # honor vetoes only while within TTL
        as_of = datetime.fromisoformat((d.get("as_of") or "").replace("Z", "+00:00"))
        ttl = float(d.get("ttl_hours", 6)) * 3600
        fresh = (datetime.now(as_of.tzinfo) - as_of).total_seconds() <= ttl
    except Exception:
        fresh = bool(d)                                 # no timestamp -> treat present file as usable
    if not d:
        return {"state": "none"}
    if fresh and (d.get("macro") or {}).get("veto_all"):
        return {"state": "veto", "veto": True, "note": (d["macro"].get("note") or "macro veto")}
    t = (d.get("tickers") or {}).get(tkr) or {}
    return {"state": "fresh" if fresh else "stale",
            "veto": bool(t.get("veto")) and fresh, "bias": t.get("bias"), "conf": t.get("conf") or 0.0}


# ─────────────────────────── entry decision (PURE, unit-testable) ───────────────────────────
def decide(tkr, ind, prev_ind, mech, session_min):
    """Given the live tape (ind), the previous lap's tape (prev_ind), the ticker's mechanics, and minutes
    since the open, return an entry spec {cp, regime, reason, score, grade} or None. Pure + deterministic
    so it can be unit-tested with synthetic indicator dicts.

    Direction comes from the desk's own tape_strength magnitude engine (pick the stronger side); a trade
    only fires on a TRIGGER — a fresh VWAP reclaim/breakdown (needs prev_ind) or strong momentum — and only
    when conviction clears the moderate bar. Midday chop (45–300 min) demands a strong grade."""
    if not ind or ind.get("rsi") is None or not ind.get("vwap") or not ind.get("price"):
        return None
    sC, sP = MK.tape_strength("C", ind), MK.tape_strength("P", ind)
    side = "C" if (sC["score"] or 0) >= (sP["score"] or 0) else "P"
    st = sC if side == "C" else sP
    if st["score"] is None or st["grade"] in ("against", "weak"):
        return None
    if st["score"] < MIN_TAPE_MODERATE:
        return None
    px, vwap = ind["price"], ind["vwap"]
    trig = None
    if prev_ind and prev_ind.get("price") and prev_ind.get("vwap"):
        pp, pv = prev_ind["price"], prev_ind["vwap"]
        if side == "C" and pp <= pv and px > vwap:
            trig = "VWAP reclaim"
        elif side == "P" and pp >= pv and px < vwap:
            trig = "VWAP breakdown"
    if not trig and st["grade"] == "strong":
        trig = "strong momentum"
    if not trig:
        return None
    if 45 < session_min < 300 and st["grade"] != "strong":     # midday chop: only high-conviction
        return None
    return {"cp": side, "regime": mech.get("regime_bias", "trend"), "reason": trig,
            "score": st["score"], "grade": st["grade"]}


# ─────────────────────────── book (one open shadow per ticker) ───────────────────────────
def _release_if_done(book, tkr):
    """Free a ticker's slot once its shadow has ended (or aged out), so chip can look for the next scalp."""
    b = book.get(tkr)
    if not b:
        return
    if time.time() - b["opened"] > MAX_HOLD_S:
        book.pop(tkr, None)
        return
    try:
        import server
        sig = next((s for s in server.SIGNALS if s.get("id") == b["sid"]), None)
    except Exception:
        return
    if not sig:
        book.pop(tkr, None)
        return
    state = (sig.get("shadow") or {}).get("state")
    if state and state != "live":
        book.pop(tkr, None)


def _cooling(tkr, now):
    return (now.timestamp() - _LAST_EMIT.get(tkr, 0)) < COOLDOWN_S


# ─────────────────────────── emit (through the normal ingest path) ───────────────────────────
def _emit(conn, tkr, dec, mech, book, now):
    """Resolve the ATM contract off the live chain, gate on spread, and emit a chip signal via ingest()."""
    import server
    spot = MK.spot_quote(tkr, ib=conn)
    if not spot:
        return
    inc = mech.get("strike_inc", 1.0)
    strike = round(spot / inc) * inc
    strike_s = str(int(strike)) if float(strike).is_integer() else str(strike)
    exp = now.strftime("%Y%m%d")                        # 0DTE — matches _resolve_expiry("0DTE") -> today
    q = MK.option_quote(tkr, strike_s, dec["cp"], expiry=exp, ib=conn)
    if not q or q.get("error") or not q.get("mid"):
        return
    entry_px = q.get("ask") or q.get("mid")             # a real fillable price -> verified, no price_mismatch
    if not entry_px:
        return
    if (q.get("spread_pct") or 99) > mech["spread_max"]:
        return                                          # too wide to scalp cleanly
    news = _news(tkr)
    data = {
        "text": f"{tkr} {strike_s}{dec['cp']} @ {round(entry_px, 2)} 0DTE",
        "provider": "chip",
        "msg_id": f"chip-{tkr}-{int(now.timestamp())}",
        "force": True,                                  # deliberate re-entry -> bypass the 4h/48h dedup sweep
        "chip_plan": {k: mech[k] for k in ("stop_pct", "t1_pct", "t2_pct", "t3_pct")},
        "chip_meta": {"regime": dec["regime"], "reason": dec["reason"], "score": dec.get("score"),
                      "news": news.get("state", "none")},
    }
    if os.environ.get("DESK_ENV") == "test":
        data["_allow_test_ingest"] = True               # sandbox only; prod bypasses the guard
    try:
        entry = server.ingest(data)
    except Exception as e:
        print(f"🤖 chip emit error {tkr}: {e}", flush=True)
        return
    if entry and entry.get("id"):
        book[tkr] = {"sid": entry["id"], "opened": time.time(), "cp": dec["cp"]}
        _LAST_EMIT[tkr] = now.timestamp()
        print(f"🤖 chip {tkr} {strike_s}{dec['cp']} @ {entry_px} "
              f"({dec['reason']}, tape {dec.get('score')}) → {entry['id']}", flush=True)


# ─────────────────────────── scan loop (daemon thread) ───────────────────────────
def scan_loop():
    conn = None
    book = {}
    prev = {}
    print("🤖 chip desk: scan loop started (shadow-only provider 'chip').", flush=True)
    while True:
        try:
            if conn is None:
                conn = MK.connect(client_id=82)
            now = _now_et()
            smin = _session_min(now)
            if smin is None:                            # market closed -> reset intraday state, idle
                prev.clear()
                book.clear()
                time.sleep(30)
                continue
            mechs = _load_mechanics()
            for tkr in TICKERS:
                try:
                    import server
                    ind = server._tape_cached(conn, tkr, ttl=60)     # shared bar cache, ≤1 pull/60s/ticker
                    mech = mechs.get(tkr, DEFAULT_MECHANICS.get(tkr, {}))
                    _release_if_done(book, tkr)
                    if tkr in book or _cooling(tkr, now):
                        prev[tkr] = ind
                        continue
                    dec = decide(tkr, ind, prev.get(tkr), mech, smin)
                    prev[tkr] = ind
                    if not dec:
                        continue
                    if _news(tkr).get("veto"):          # hard news/macro veto -> suppress
                        continue
                    _emit(conn, tkr, dec, mech, book, now)
                except Exception:
                    continue
            try:
                MK.reap_subs(conn, cap=80)
            except Exception:
                pass
            time.sleep(LOOP_SLEEP)
        except Exception as e:
            print(f"🤖 chip desk loop error: {e} — reconnecting", flush=True)
            conn = None
            time.sleep(10)
