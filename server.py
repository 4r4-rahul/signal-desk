#!/usr/bin/env python3
"""
Signal Desk server — receives live Discord signals, runs the engine, and serves a
web dashboard with live cards + Take/Skip (which log to the tracker).

Run:   python3 server.py         (open http://localhost:8787 in a browser)
Env:   ACCOUNT=15000 RISK_PCT=1  python3 server.py
"""
import json, http.server
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os, re
import signal_engine as E
import tracker as T
import discipline as D
import equity as EQ
import validate as V
import integrations as N
import manage as M
import stats as ST
try:
    import db as DB                                    # SQLite warehouse — every signal/decision/outcome
except Exception:
    DB = None
try:
    import broker as BROKER                            # tastytrade — live account feed + disciplined order gateway
except Exception:
    BROKER = None


def _broker_snap():
    """Cached tastytrade account (balance + positions) for the desk. Silent if not configured."""
    if not BROKER:
        return None
    try:
        snap = BROKER.account_snapshot()
        if snap and not snap.get("error"):
            snap = {**snap, "sandbox": os.environ.get("TT_TEST", "1") == "1"}
        return snap
    except Exception:
        return None

_CALIB_CACHE = {"t": 0.0, "calibration": None, "exits": None}


def _scorecard_cached():
    """Score calibration + exit quality for the desk — recomputed at most every 20s (no need per poll)."""
    if not DB:
        return None, None
    import time
    if time.time() - _CALIB_CACHE["t"] > 20:
        try:
            _CALIB_CACHE["calibration"] = DB.calibration()
        except Exception:
            _CALIB_CACHE["calibration"] = None
        try:
            _CALIB_CACHE["exits"] = DB.exit_quality()
        except Exception:
            _CALIB_CACHE["exits"] = None
        try:                                              # persist data-gated provider size tiers for the engine
            _edge = DB.provider_edge()
            (ROOT / "channels" / "_provider_edge.json").write_text(json.dumps(_edge))
            _CALIB_CACHE["edge"] = _edge                   # cache for the dashboard payload (no per-poll DB hit)
        except Exception:
            pass
        _CALIB_CACHE["t"] = time.time()
    return _CALIB_CACHE["calibration"], _CALIB_CACHE["exits"]


def _provider_edge_cached():
    """The blended per-provider edge dict (realized+shadow -> size mult), from the 20s cache."""
    return _CALIB_CACHE.get("edge") or {}
ENRICH = os.environ.get("ENRICH_IBKR", "1") == "1"   # live technicals from IBKR
START_ACCOUNT = E.ACCOUNT                              # the strategy allocation (env ACCOUNT)
# compounding source: allocation sleeve (default), full ibkr balance, or fixed
EQUITY_SOURCE = os.environ.get("EQUITY_SOURCE", "allocation")

PORT = 8787


SLEEVE = {"equity": START_ACCOUNT, "hwm": START_ACCOUNT, "drawdown": 0.0, "throttle": 1.0, "halted": False}
ALERTED = {"killed": set()}


def touch_source(key, provider=None, desc=None, display=None, healthy=None, seen=None):
    """Register/update a listening channel (keyed by unique channel id): heartbeat + stats.
    `healthy` is the extension's SCRAPE health (did it find Discord's message list this tick?) — distinct
    from the mere heartbeat. A relayed message (desc) is itself proof the scrape works, so it marks healthy."""
    now = datetime.now(ET)
    s = SOURCES.setdefault(key, {"name": display or key, "count": 0, "first": now.isoformat()})
    s["last_beat"] = now.isoformat()
    if display:
        s["name"] = display
    if provider:
        s["provider"] = provider
    if healthy is not None:                       # heartbeat carried a scrape-health flag
        s["healthy"] = bool(healthy)
        s["scan_seen"] = seen
        if healthy:
            s["last_healthy"] = now.isoformat()
    if desc:
        s["count"] += 1
        s["last_signal"] = desc
        s["last_signal_iso"] = now.isoformat()
        s["healthy"] = True                        # a message got through -> scraping is provably alive
        s["last_healthy"] = now.isoformat()


def _alert(text, title):
    try:
        N.send_alert(text, title)
    except Exception:
        pass


def _signal_alert(e):
    """Push the FULL info of a new card to Discord immediately — everything on the UI, one message."""
    if not e:
        return
    sg = e.get("sig") or {}; pl = e.get("plan") or {}; tk = e.get("ticket") or {}
    o = e.get("opt") or {}; gr = e.get("grade") or {}; tech = e.get("tech") or {}
    leg = f"{sg.get('ticker')} {sg.get('strike')}{sg.get('type')}"
    L = [f"**{leg}**  ·  exp {sg.get('expiry') or '0DTE'}  ·  provider entry **${sg.get('premium')}**"]
    if e.get("watch"):
        L.append("👀 **WATCHING** — provider not filled yet")
    tags = []
    if gr.get("g"):
        tags.append(f"🎓 Grade {gr['g']} ({gr.get('score')})")
    if e.get("tier_key"):
        tags.append(f"{e['tier_key']} · conf {e.get('score')}")
    if tags:
        L.append(" · ".join(tags))
    if tk.get("limit") is not None:
        L.append(f"🎯 **Limit ${tk['limit']}** (bid {tk.get('bid')} / ask {tk.get('ask')})   "
                 f"🛑 **Stop ${tk['stop']}** (−{tk.get('stop_pct')}%)")
        L.append(f"🥇 T1 ${tk.get('t1')} (+{tk.get('t1_pct')}%)  ·  🥈 T2 ${tk.get('t2')} (+{tk.get('t2_pct')}%)  "
                 f"·  🏆 T3 ${tk.get('t3')} (+{tk.get('t3_pct')}%)")
        if tk.get("rr3") is not None:
            L.append(f"⚖️ R:R {tk['rr1']} → {tk['rr2']} → {tk['rr3']}×")
    if pl.get("ok"):
        L.append(f"🎲 Risk **{pl.get('risk_pct')}%** (~${round(pl.get('risk', 0))}) → "
                 f"**{pl.get('contracts')}** contracts · {pl.get('risk_basis')}")
    micro = []
    if o.get("iv") is not None:
        micro.append(f"IV {round(o['iv'] * 100)}%")
    if o.get("delta") is not None:
        micro.append(f"Δ{round(o['delta'], 2)}")
    if o.get("theta") is not None and o.get("mid"):
        micro.append(f"θ {round(abs(o['theta']) / o['mid'] * 100)}%/day")
    if tech.get("label") and tech.get("label") != "no data":
        micro.append(f"tape {tech['label']}")
    if tk.get("spread_pct") is not None:
        micro.append(f"spread {tk['spread_pct']}%")
    if micro:
        L.append("📊 " + "  ·  ".join(micro))
    flags = []
    if tk.get("chasing_pct"):
        flags.append(f"⚠ chasing +{tk['chasing_pct']}%")
    if tk.get("wide"):
        flags.append("⚠ wide spread")
    if sg.get("lotto"):
        flags.append("🎰 lotto")
    if e.get("cosign"):
        flags.append("🔗 also: " + ", ".join(e["cosign"]))
    if flags:
        L.append("  ·  ".join(flags))
    L.append(f"\n📝 _{(e.get('raw') or '')[:220]}_")
    L.append("🖥️ http://localhost:8787")
    _alert("\n".join(L), f"🔔 New signal — {e.get('provider', '?')}")


def check_alerts():
    """Edge-triggered webhook alerts on capital-critical events (fire once per change)."""
    due = SLEEVE.get("milestone_due", 0) or 0
    if due > 100 and ALERTED.get("milestone") != round(due):
        _alert(f"💰 **Milestone** — bank ${due:,.0f} to reserve. Peak ${SLEEVE.get('peak',0):,.0f}, "
               f"sleeve ${SLEEVE.get('equity',0):,.0f}.", "Signal Desk — Milestone")
        ALERTED["milestone"] = round(due)
    if due <= 100:
        ALERTED["milestone"] = None
    for p, e in V.killed_providers().items():
        if p not in ALERTED["killed"]:
            _alert(f"⛔ Provider **{p}** auto-disabled — last-20 expectancy {e}% (negative).",
                   "Signal Desk — Kill Switch")
            ALERTED["killed"].add(p)
    if SLEEVE.get("halted"):
        if not ALERTED.get("halted"):
            _alert(f"🛑 **−20% DRAWDOWN HALT** — sleeve ${SLEEVE.get('equity',0):,.0f} vs HWM "
                   f"${SLEEVE.get('hwm',0):,.0f}. Trading stopped, manual review.", "Signal Desk — HALT")
            ALERTED["halted"] = True
    else:
        ALERTED["halted"] = False
    ds = D.state()
    if ds["blocked"]:
        key = tuple(ds["reasons"])
        if ALERTED.get("daystop") != key:
            _alert(f"🛑 **Day stopped** — {', '.join(ds['reasons'])}.", "Signal Desk — Day Stop")
            ALERTED["daystop"] = key
    else:
        ALERTED["daystop"] = None


def refresh_account(ibkr_equity=None):
    global SLEEVE
    if EQUITY_SOURCE == "allocation":
        try:
            SLEEVE = EQ.sleeve_state(START_ACCOUNT)        # compound sleeve + HWM/drawdown throttle
            E.ACCOUNT = SLEEVE["sizing_base"]              # size off speed-capped base
            E.THROTTLE = SLEEVE["throttle"]                # geometric de-risk in drawdown
        except Exception:
            pass
    elif EQUITY_SOURCE == "ibkr" and ibkr_equity:
        E.ACCOUNT = ibkr_equity                           # size off full broker balance
ROOT = Path(__file__).parent
SEG = E.load_segments()
ET = timezone(timedelta(hours=-4))


def load_chmap():
    """channel-key -> provider, so a provider's channel is tagged even if the poster's name differs."""
    try:
        return json.loads((ROOT / "channels" / "_channel_map.json").read_text())
    except Exception:
        return {}


CHMAP = load_chmap()
SIGNALS_STORE = ROOT / "channels" / "_signals.json"
INBOUND_LOG = ROOT / "logs" / "inbound.log"


def load_signals():
    try:
        return json.loads(SIGNALS_STORE.read_text())
    except Exception:
        return []


def save_signals():
    try:
        SIGNALS_STORE.write_text(json.dumps(SIGNALS[-250:]))
    except Exception:
        pass


def _resolve_expiry(sig):
    """Convert a signal's PARSED expiry (0DTE / 7/7 / Jul 7 / TOMORROW / NDTE) to an IBKR yyyymmdd date,
    so a dated (multi-day) trade is priced on its REAL contract, not the nearest expiry. None -> let IBKR pick."""
    now = datetime.now(ET)
    exp = (sig.get("expiry") or "").strip().upper()
    dte = sig.get("dte")
    if exp == "0DTE" or dte == 0:
        return now.strftime("%Y%m%d")
    if exp == "TOMORROW" or dte == 1:
        return (now + timedelta(days=1)).strftime("%Y%m%d")
    if isinstance(dte, int) and dte > 1:
        return (now + timedelta(days=dte)).strftime("%Y%m%d")
    m = re.match(r"(\d{1,2})/(\d{1,2})$", exp)                      # MM/DD
    if not m:
        mm = re.match(r"([A-Z]{3,})\.?\s*(\d{1,2})$", exp)          # "JUL 7"
        if mm:
            MO = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
                  "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
            mo = MO.get(mm.group(1)[:3]); dd = int(mm.group(2))
            m = None if mo is None else (mo, dd)
        else:
            m = None
    else:
        m = (int(m.group(1)), int(m.group(2)))
    if m:
        mo, dd = m
        for yr in (now.year, now.year + 1):
            try:
                d = datetime(yr, mo, dd, tzinfo=ET)
                if d.date() >= now.date():
                    return d.strftime("%Y%m%d")
            except ValueError:
                return None
    return None


def _archive_signal(prov, entry):
    """PERMANENT structured archive of every signal — the FULL CARD (sig + score + reasons + the whole
    sizing plan + dynamic stop & its terms + behavior + Greeks). Never pruned. The dataset for future
    analysis / expectancy tracking / honest ML retraining on real live data + real recommendations."""
    try:
        d = ROOT / "channels" / (prov or "unknown")
        d.mkdir(parents=True, exist_ok=True)
        rec = {k: v for k, v in entry.items() if k != "tech"}   # everything except the bulky raw tech blob
        rec["ts_utc"] = datetime.now(timezone.utc).isoformat()  # explicit UTC, matches historical data
        rec["tech_label"] = (entry.get("tech") or {}).get("label")
        rec["indicators"] = (entry.get("tech") or {}).get("ind")   # keep the derived indicators, drop raw bars
        with (d / "live_signals.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _log_paper_entry(prov, sid, entry):
    """Append a PAPER-mode hypothetical entry so distrusted providers build a track record
    (graduation gate reads channels/<prov>/paper_journal.csv). Outcome filled later by you."""
    try:
        import csv as _csv
        pj = ROOT / "channels" / prov / "paper_journal.csv"
        pj.parent.mkdir(parents=True, exist_ok=True)
        newf = not pj.exists()
        sig = entry.get("sig", {})
        with pj.open("a", newline="") as f:
            w = _csv.writer(f)
            if newf:
                w.writerow(["id", "alert_ts", "ticker", "strike", "type", "paper_entry", "status", "my_return_pct", "notes"])
            w.writerow([sid, entry.get("iso"), sig.get("ticker"), sig.get("strike"), sig.get("type"),
                        sig.get("premium") or "", "PAPER", "", f"conf={entry.get('score')}"])
    except Exception:
        pass


def _inlog(channel, prov, text, outcome):
    """Log EVERY inbound message + what happened to it — so we can see per-channel capture."""
    try:
        INBOUND_LOG.parent.mkdir(exist_ok=True)
        with INBOUND_LOG.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} [{(prov or '?'):<12}|{channel or '?'}] "
                    f"{outcome:<8} {text[:120]!r}\n")
    except Exception:
        pass


SIGNALS = load_signals()   # survive restarts — don't wipe the day's cards
SOURCES = {}            # provider -> last-seen HH:MM:SS (which channels are streaming)
PENDING = []            # unlinked management msgs awaiting disambiguation


def _looks_mgmt(t):
    t = t.lower()
    return len(t) < 160 and bool(re.search(
        r"\d|half|some|most|reduce|close|exit|dump|lose|runner|secure|lock|flat|trim|cut|stop|out\b", t))


# ================= LIVE POSITION MONITOR (IBKR market data) =================
MONITOR = {}    # pos_id -> {mid, pnl_pct, peak_pct, iv, delta, spread_pct, expiry, alert, fired}
SPOT_CACHE = {} # ticker -> (epoch, price) — fast underlying last-price (ustop_loop), for stop-watch + card
LIVE_SIG = {}   # card_id -> live quote + move% + recommendation for an un-taken actionable card
_TAPE_CACHE = {}   # ticker -> (epoch, indicators) — throttled underlying spot+tape for path capture
DAY_OVERRIDE_DATE = None   # ISO date the day-stop is manually overridden (auto-resets each day)
_OVERRIDE_FILE = ROOT / "channels" / "_day_override"   # persisted so a restart doesn't silently re-block


def _save_override():
    try:
        if DAY_OVERRIDE_DATE:
            _OVERRIDE_FILE.write_text(DAY_OVERRIDE_DATE)
        else:
            _OVERRIDE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _load_override():
    global DAY_OVERRIDE_DATE
    try:
        DAY_OVERRIDE_DATE = _OVERRIDE_FILE.read_text().strip() or None
    except Exception:
        DAY_OVERRIDE_DATE = None


_load_override()


def _day_override_active():
    return DAY_OVERRIDE_DATE == datetime.now(ET).date().isoformat()


def _live_reco(prov_entry, mid, ask, spread_pct):
    """Live 'should I still take this?' read for an un-taken card: how far the option has moved
    since the provider's signal, and a recommendation gated on the chase (cost to enter NOW)."""
    if not prov_entry or mid is None:
        return None
    move = round((mid / prov_entry - 1) * 100)                       # premium move since signal (mid)
    chase = round((ask / prov_entry - 1) * 100) if ask else move     # cost to enter now (ask vs signal)
    wide = bool(spread_pct and spread_pct > 15)
    cheap = prov_entry < 0.30                                        # cheap 0DTE tolerates more % chase
    ok, hard = (12, 35) if cheap else (8, 25)                        # buy-zone ceiling, hard skip ceiling
    if chase <= 0:
        if chase >= -15:                                            # small dip — genuinely a better entry
            lvl, txt = "better", f"🟢 {abs(chase)}% cheaper than the signal — better entry, thesis intact"
        elif chase >= -40:                                          # faded hard — momentum against it
            lvl, txt = "caution", f"⚠ Faded {chase}% since signal — momentum against it, be careful"
        else:                                                       # collapsed — the setup likely failed
            lvl, txt = "skip", f"🚫 Down {chase}% since signal — thesis likely broken, skip"
    elif chase <= ok:
        lvl, txt = "go", f"✅ In buy zone (+{chase}%) — take at the limit"
    elif chase <= hard:
        lvl, txt = "caution", f"⚠ Paying up +{chase}% — smaller size, tighten stop"
    else:
        lvl, txt = "skip", f"🚫 Too far +{chase}% — skip / wait for a pullback"
    if wide and lvl != "skip":
        txt += " · wide spread, limit only"
    return {"move_pct": move, "chase_pct": chase, "mid": mid, "ask": ask,
            "wide": wide, "level": lvl, "text": txt}


def _path_tick(signal_id, phase, q, ticker, ref, state, start_utc, conn):
    """Snapshot the FULL conditions at one tick of a trade's life (shadow OR position) -> path table.
    Layers: option greeks + underlying spot/move + tape (RSI/VWAP/EMA/BB) + time + momentum + peak flags."""
    if not DB or not signal_id or not ref or not q.get("mid"):
        return
    import market as MK, time as _t
    pct = round((q["mid"] / ref - 1) * 100, 1)
    tp = _TAPE_CACHE.get(ticker)
    if not tp or _t.time() - tp[0] > 180:                # throttle underlying bars fetch to ~3 min
        try:
            _ind = MK.indicators(MK.get_bars(conn, ticker)) or {}
        except Exception:
            _ind = {}
        _TAPE_CACHE[ticker] = (_t.time(), _ind)
    ind = _TAPE_CACHE[ticker][1]
    spot = ind.get("price")
    if state.get("entry_spot") is None and spot:
        state["entry_spot"] = spot
    es, prev = state.get("entry_spot"), state.get("prev_pct")
    mom = ("up" if (prev is not None and pct > prev + 2) else
           "down" if (prev is not None and pct < prev - 2) else "flat")
    pk = max(state.get("_pk", pct), pct); state["_pk"] = pk
    tr = min(state.get("_tr", pct), pct); state["_tr"] = tr
    et = datetime.now(ET)
    try:
        mins = round((datetime.now(timezone.utc) - datetime.fromisoformat(start_utc)).total_seconds() / 60, 1) if start_utc else None
    except Exception:
        mins = None
    DB.record_tick({
        "signal_id": signal_id, "ts_utc": datetime.now(timezone.utc).isoformat(), "phase": phase,
        "mins": mins, "tod_min": et.hour * 60 + et.minute,
        "opt_mid": q["mid"], "opt_pct": pct, "bid": q.get("bid"), "ask": q.get("ask"),
        "iv": q.get("iv"), "delta": q.get("delta"), "gamma": q.get("gamma"),
        "theta": q.get("theta"), "vega": q.get("vega"),
        "spot": spot, "spot_pct": (round((spot / es - 1) * 100, 2) if (spot and es) else None),
        "rsi": ind.get("rsi"), "vwap": ind.get("vwap"), "ema9": ind.get("ema9"), "ema21": ind.get("ema21"),
        "bb_up": ind.get("bb_up"), "bb_low": ind.get("bb_low"),
        "momentum": mom, "is_peak": 1 if pct >= pk else 0, "is_trough": 1 if pct <= tr else 0})
    state["prev_pct"] = pct


def _init_shadow(entry):
    """Begin shadow-tracking a SKIPPED signal: from here, did it hit OUR T1 (win) or OUR stop (loss)?
    Records the reference price + our stop/target so the monitor can decide the would-be outcome."""
    if entry.get("shadow"):
        return
    tk, sg = entry.get("ticket") or {}, entry.get("sig") or {}
    ref = tk.get("limit") or sg.get("premium")
    if not ref:
        return
    entry["shadow"] = {"ref": ref, "stop_pct": (entry.get("plan") or {}).get("stop_pct") or tk.get("stop_pct") or 30,
                       "t1_pct": tk.get("t1_pct") or 30,
                       "expiry": _resolve_expiry(sg) or (entry.get("opt") or {}).get("expiry"),
                       "start_utc": datetime.now(timezone.utc).isoformat(), "state": "live",
                       "result": None, "peak": 0.0, "trough": 0.0, "now": 0.0}


def _pos_alert(p, st, key, text):
    if key in st["fired"]:
        return
    st["fired"].add(key)
    st["alert"] = text
    M.log_live_alert(p["id"], text, st.get("peak_pct"))     # persist for the lifetime record
    _alert(f"{p['provider']} {p['ticker']} {p['leg']}: {text}  (live ${st.get('mid')})",
           "Signal Desk — LIVE EXIT")


def _round_trip_setup(p):
    """Gamma-heavy setups that spike then ROUND-TRIP to a loss (data: 4/5 whipsaws were these):
    a lotto, or a far-OTM low-delta contract. Bank the pop hard on these."""
    sg = (p.get("origin") or {}).get("sig") or {}
    dl = (p.get("entry_greeks") or {}).get("delta")
    return bool(sg.get("lotto")) or (dl is not None and abs(dl) < 0.30)


def _check_ustop(p, st):
    """PROVIDER'S UNDERLYING STOP (e.g. JMT "SL:751.60") — the real rule the provider trades. Watch live
    spot vs the level, independent of the option premium AND of the message feed: a call stops when
    spot <= level, a put when spot >= level. Protects the trade even if Discord goes dark. Called by the
    FAST spot loop (near-real-time) and the slow monitor loop; _pos_alert dedupes so it fires once."""
    us, spot = p.get("u_stop"), st.get("spot")
    if not (us and spot):
        return
    is_call = (p.get("leg") or "")[-1:].upper() != "P"
    if (spot <= us) if is_call else (spot >= us):
        _pos_alert(p, st, "ustop",
                   f"🔴 {p['ticker']} {spot:.2f} crossed the provider stop {us:.2f} — CUT NOW")


def _check_pos_rules(p, st):
    from datetime import datetime
    pnl, peak = st.get("pnl_pct", 0), st.get("peak_pct", 0)
    _check_ustop(p, st)
    if pnl <= -45:
        _pos_alert(p, st, "stop", "🛑 STOP −45% hit — exit now")
    elif _round_trip_setup(p) and peak >= 35 and p["state"] == "OPEN":
        _pos_alert(p, st, "bankpop", f"⚡ +{peak:.0f}% on a lotto/far-OTM — BANK PARTIAL NOW, these round-trip to a loss!")
    elif pnl >= 40 and p["state"] == "OPEN":
        _pos_alert(p, st, "tp1", "🎯 +40% — T1 hit: scale out the base, stop to breakeven")
    if pnl >= 75 and p["state"] not in ("CLOSED", "CUT"):
        _pos_alert(p, st, "tp2", "🎯 +75% — T2 hit: trim again, trail the rest")
    if pnl >= 110 and p["state"] not in ("CLOSED", "CUT"):
        _pos_alert(p, st, "tp3", "🎯 +110% — T3: take the rest, don't round-trip a winner")
    if p["state"] == "RUNNER" and peak >= 30 and pnl <= peak - 25:
        _pos_alert(p, st, "trail", f"📉 runner gave back — was +{peak:.0f}%, now +{pnl:.0f}% — exit")
    hhmm = datetime.now(ET).hour * 100 + datetime.now(ET).minute
    if hhmm >= 1545:
        _pos_alert(p, st, "eod", "⏰ EOD — flat 0DTE before the close")


def _tape_cached(conn, ticker, ttl=60):
    """Throttled underlying indicators (RSI/VWAP/EMA/BB/price). Shares _TAPE_CACHE with the path tracker."""
    import market as MK, time as _t
    tp = _TAPE_CACHE.get(ticker)
    if not tp or _t.time() - tp[0] > ttl:
        try: ind = MK.indicators(MK.get_bars(conn, ticker)) or {}
        except Exception: ind = {}
        _TAPE_CACHE[ticker] = (_t.time(), ind)
    return _TAPE_CACHE[ticker][1]


def _tape_align(cp, ind):
    """Does the underlying TAPE support this option's direction? Scores EMA trend, VWAP side, RSI, and
    flags 'extended' (price hugging the band in the option's favor = little room left). Returns a dict."""
    is_call = (cp or "C").upper() != "P"
    px, vwap, e9, e21, rsi = ind.get("price"), ind.get("vwap"), ind.get("ema9"), ind.get("ema21"), ind.get("rsi")
    bbu, bbl = ind.get("bb_up"), ind.get("bb_low")
    score, bullets = 0, []
    if e9 and e21:
        up = e9 > e21; good = up if is_call else not up
        score += 1 if good else -1
        bullets.append(("✅" if good else "❌") + f" EMA9 {round(e9,2)} {'>' if up else '<'} EMA21 {round(e21,2)} — {'up' if up else 'down'}trend")
    if px and vwap:
        above = px > vwap; good = above if is_call else not above
        score += 1 if good else -1
        bullets.append(("✅" if good else "❌") + f" price {'above' if above else 'below'} VWAP {round(vwap,2)}")
    if rsi is not None:
        good = (45 <= rsi <= 72) if is_call else (28 <= rsi <= 55)
        note = "healthy" if good else (("overbought" if rsi > 72 else "weak") if is_call else ("oversold" if rsi < 28 else "weak"))
        score += 1 if good else -1
        bullets.append(("✅" if good else "⚠️") + f" RSI {round(rsi)} — {note}")
    extended = False
    if px and bbu and bbl and bbu > bbl:
        if is_call and px >= bbu - (bbu - bbl) * 0.12:
            extended = True; bullets.append(f"⚠️ near upper Bollinger {round(bbu,2)} — extended, limited room")
        elif (not is_call) and px <= bbl + (bbu - bbl) * 0.12:
            extended = True; bullets.append(f"⚠️ near lower Bollinger {round(bbl,2)} — extended, limited room")
    verdict = "ALIGNED" if score >= 2 else ("AGAINST" if score <= -2 else "MIXED")
    return {"verdict": verdict, "score": score, "bullets": bullets, "extended": extended,
            "vwap": round(vwap, 2) if vwap else None, "price": round(px, 2) if px else None}


def _roll_strike(ticker, cp, exp, spot, conn, target_delta=0.45):
    """When the provider's original strike has run too far ITM to chase, find a FRESH near-the-money
    strike whose delta ≈ target (≈0.45 = slightly OTM, good leverage at a sane premium) — the SAME
    directional thesis expressed at a better entry. Quotes a small ladder around spot, picks the closest
    delta. Returns {'strike', 'quote'} or None."""
    import market as MK
    if not spot:
        return None
    inc = 5.0 if spot >= 100 else (2.5 if spot >= 40 else 1.0)
    base = round(spot / inc) * inc
    best = None
    for i in (-2, -1, 0, 1, 2, 3):
        k = base + inc * i
        if k <= 0:
            continue
        ks = str(int(k)) if float(k).is_integer() else str(k)
        q = MK.option_quote(ticker, ks, cp, expiry=exp, ib=conn)
        if not q or q.get("error") or q.get("delta") is None:
            continue
        if best is None or abs(abs(q["delta"]) - target_delta) < abs(abs(best["quote"]["delta"]) - target_delta):
            best = {"strike": ks, "quote": q}
        try: conn.sleep(0.4)
        except Exception: pass
    return best


def reentry_read(ticker, strike, cp, exp, q, ind, ref_exit=None, u_stop=None, orig_premium=None, rolled=None):
    """Combine the live option quote + tape into a CONDITIONAL re-entry verdict with an executable
    trigger. ref_exit = your last exit (so re-entry isn't blindly above it); u_stop = provider SL level.
    orig_premium = the provider's original fill, to flag when the SAME strike has run too far to chase
    (→ a roll to a fresh strike beats it). rolled = {'from': strike} when this IS a fresh rolled strike."""
    if not q or q.get("mid") is None:
        # ALWAYS a truthy string — an {error: None} once crashed the whole feed render client-side
        return {"error": (q.get("error") if q else None) or "no live quote (expired contract? dial the strike/expiry)"}
    mid, ask, bid = q.get("mid"), q.get("ask"), q.get("bid")
    theta, delta, iv, spr = q.get("theta"), q.get("delta"), q.get("iv"), q.get("spread_pct")
    al = _tape_align(cp, ind)
    import market as _MK
    al["strength"] = _MK.tape_strength(cp, ind)              # graded conviction, same meter as the cards
    dte = None
    try:
        d = datetime.strptime(exp, "%Y%m%d").date(); dte = (d - datetime.now(ET).date()).days
    except Exception:
        pass
    theta_pct = round(abs(theta) / mid * 100) if (theta and mid) else None
    onedte = dte is not None and dte <= 1
    limit = round(mid * 0.90, 2)                                   # buy a dip, don't chase
    if ref_exit:
        try: limit = min(limit, round(float(ref_exit) - 0.05, 2))  # never re-buy at/above your exit
        except Exception: pass
    trigger = None
    if al["verdict"] == "AGAINST":
        verdict, vcol, headline = "BACK OFF", "red", "tape is against this direction — do not re-enter"
    elif al["verdict"] == "MIXED":
        verdict, vcol, headline = "WAIT", "amber", "tape mixed — no edge yet, let it pick a side"
    elif al["extended"] or onedte:
        verdict, vcol = "CONDITIONAL", "amber"
        why = " & ".join([w for w in (("extended" if al["extended"] else ""), ("1-DTE theta" if onedte else "")) if w])
        headline = f"tape's with you but {why}"
        trigger = (f"arm a ${limit} pullback-limit — fill ONLY while price holds above VWAP {al.get('vwap')} "
                   f"and EMA9>EMA21. If it loses {al.get('vwap')}, cancel (breakdown, not a dip). 1 lot / lotto size.")
    else:
        verdict, vcol, headline = "RE-ENTER", "green", "tape aligned and not extended — clean re-entry"
        trigger = f"take at the ask (~${ask}) or a ${limit} limit; stop {('SPY '+str(u_stop)) if u_stop else 'per plan'}."
    # CHASE: is the SAME strike now too far above the provider's original fill to be worth chasing?
    chase_pct = round((mid / orig_premium - 1) * 100) if (orig_premium and mid and not rolled) else None
    chasing = chase_pct is not None and chase_pct >= 30
    if chasing and verdict in ("RE-ENTER", "CONDITIONAL"):
        headline += f" — but this strike is +{chase_pct}% since the call (chasing); a fresh strike beats it"
    exps = f"{exp[4:6]}/{exp[6:]}" if (exp and len(exp) == 8) else exp
    return {"contract": f"{ticker} {strike}{cp} {exps}",
            "option": {"mid": mid, "bid": bid, "ask": ask, "delta": delta, "iv": iv, "theta": theta,
                       "theta_pct": theta_pct, "spread_pct": spr, "dte": dte, "onedte": onedte},
            "tape": al, "ref_exit": ref_exit, "u_stop": u_stop, "limit": limit,
            "verdict": verdict, "vcol": vcol, "headline": headline, "trigger": trigger,
            "chase_pct": chase_pct, "chasing": chasing, "rolled": rolled}


def entry_timing(sig, ind, reco, u_stop=None, spot=None):
    """ONE clear 'when do I pull the trigger' verdict for an actionable card — synthesizes tape STRENGTH,
    buy-zone/chase, and stop proximity into TAKE / WAIT / SKIP with a concrete watchable trigger, so you
    never have to interpret the raw signals. Universal across every actionable card (and re-entry)."""
    import market as _MK
    call = (sig.get("type") or "C").upper() != "P"
    tk, prem = sig.get("ticker"), sig.get("premium")
    st = _MK.tape_strength(sig.get("type"), ind) if ind else {"grade": "no-data"}
    spot = spot if spot is not None else (ind or {}).get("price")
    vwap = (ind or {}).get("vwap")
    vw = f"{vwap:.2f}" if vwap else "VWAP"
    chase = (reco or {}).get("chase_pct", 0)
    reclaim = f"reclaims VWAP {vw} and holds" if call else f"loses VWAP {vw} and holds"
    holds = f"holds above VWAP {vw} & RSI lifts" if call else f"holds below VWAP {vw} & RSI drops"
    def out(a, i, h, t=None): return {"action": a, "icon": i, "head": h, "trig": t}
    if u_stop and spot and ((spot <= u_stop) if call else (spot >= u_stop)):        # thesis broken
        return out("SKIP", "🔴", f"{tk} {spot:.2f} is past the {u_stop} stop — thesis broken")
    if chase <= -40:                                                                # collapsed since the call
        return out("SKIP", "🔴", f"down {chase}% since the call — the setup likely failed")
    if st.get("grade") == "against":                                               # tape against direction
        return out("WAIT", "🔴", "tape is AGAINST this direction right now", f"take when {tk} {reclaim}")
    # STRENGTH-AWARE chase gate: a STRONG confirmed tape justifies paying up a bit (fast runners used to
    # be structurally un-takeable — price left the zone before the tape confirmed, e.g. SPY 753C +78%).
    _cap = 35 if (prem or 1) < 0.5 else (32 if st.get("grade") == "strong" else 22)
    if chase >= _cap:                                                               # chasing too far even so
        _trig = f"take on a pullback to ~${round((prem or 0) * 1.08, 2)}"
        if st.get("grade") in ("strong", "moderate"):
            _trig += " — or 🎯 roll to a fresh strike (tape's confirmed, the called strike just ran)"
        return out("WAIT", "🟡", f"chasing +{chase}% above the call — paying up", _trig)
    if u_stop and spot and abs(spot - u_stop) / spot < 0.0015:                      # no room to the stop
        return out("WAIT", "🟡", f"right on top of the {u_stop} stop — no room", f"take once {tk} pushes ~0.3 off {u_stop}")
    if st.get("grade") in ("weak", "no-data"):                                     # tape flat / no push
        return out("WAIT", "⚪", "tape aligned but weak/flat — no push yet", (f"take when {tk} {holds}" if vwap else "take when the tape strengthens"))
    return out("TAKE", "🟢", f"tape {st.get('grade')}, in buy zone ({chase:+d}%), room to the stop", "take at the limit now")


def monitor_loop():
    import market as MK
    import time
    conn = None
    while True:
        try:
            if conn is None:
                conn = MK.connect(client_id=80)
                try: conn.reqMarketDataType(int(MK.env("IBKR_MARKET_DATA_TYPE", "2")))
                except Exception: pass
            for p in M.open_positions():
                try:
                    leg = p.get("leg") or ""
                    if not p.get("ticker") or len(leg) < 2:
                        continue
                    st = MONITOR.setdefault(p["id"], {"fired": set()})
                    if "expiry" not in st and p.get("expiry"):     # price the position's REAL expiry, not nearest
                        st["expiry"] = p["expiry"]
                    q = MK.option_quote(p["ticker"], leg[:-1], leg[-1], expiry=st.get("expiry"), ib=conn)
                    if not q or q.get("error") or not q.get("mid"):
                        continue
                    entry = p.get("avg_entry") or p.get("entry") or 0
                    # Contract-mismatch guard: a live price an order of magnitude off the entry (and itself
                    # large) means the resolved contract isn't this trade — a misparsed strike/expiry or an
                    # impossible provider premium (a deep-ITM 375C can't cost $2.9). Suppress the fantasy P&L
                    # instead of marking the position at +9000% / a fake $88k open gain.
                    if entry and ((q["mid"] > entry * 8 and q["mid"] > 20) or (q["mid"] * 8 < entry and entry > 20)):
                        st.update(mid=q["mid"], pnl_pct=None,
                                  mismatch={"entry": round(entry, 2), "live": q["mid"]})
                        continue
                    st.pop("mismatch", None)
                    pnl = (q["mid"] / entry - 1) * 100 if entry else 0
                    st.update(expiry=q.get("expiry", st.get("expiry")), mid=q["mid"], pnl_pct=round(pnl, 1),
                              iv=q.get("iv"), delta=q.get("delta"), spread_pct=q.get("spread_pct"))
                    st["peak_pct"] = round(max(st.get("peak_pct", pnl), pnl), 1)
                    M.update_peak(p["id"], st["peak_pct"])       # persist the market high
                    _path_tick(p.get("journal_id"), "position", q, p["ticker"], entry, st,
                               p.get("opened_utc") or p.get("opened"), conn)   # full conditions of the LIVE trade
                    _fast = SPOT_CACHE.get(p["ticker"])                              # fast poll wins if present
                    _sp = _fast[1] if _fast else (_TAPE_CACHE.get(p["ticker"]) or (0, {}))[1].get("price")
                    if _sp:
                        _prev = st.get("spot")
                        if _prev is not None and abs(_sp - _prev) > 1e-9:
                            st["spot_dir"] = "up" if _sp > _prev else "down"          # tick-over-tick direction
                        st["spot"] = _sp                                              # -> stop-watch + card display
                    _check_pos_rules(p, st)
                    conn.sleep(1)
                except Exception:
                    continue
            # live-quote the ACTIONABLE un-taken cards -> live move% + recommendation on the card
            act = [s for s in SIGNALS if s.get("status") in ("NEW", "WATCHING") and s.get("verified")
                   and (not s.get("paper") or s.get("manual_override")) and (s.get("sig") or {}).get("premium")]
            for s in act[-20:]:                              # cap: 20 most-recent actionable cards
                try:                                          # one bad/ambiguous contract must NOT kill the cycle
                    sg = s["sig"]
                    exp = _resolve_expiry(sg) or (s.get("opt") or {}).get("expiry")   # SIGNAL'S expiry wins
                    q = MK.option_quote(sg["ticker"], str(sg["strike"]).split(".")[0], sg["type"],
                                        expiry=exp, ib=conn)
                    if not q or q.get("error") or q.get("mid") is None:
                        continue
                    if (s.get("opt") or {}).get("expiry") != q.get("expiry"):   # heal a wrong-expiry snapshot
                        s["opt"] = q
                        _pl = s.get("plan") or {}
                        _e = _pl.get("entry") or sg.get("premium") or 0
                        _t2f = (_pl["tp2"] / _e - 1) if (_e and _pl.get("tp2")) else 0.70
                        s["ticket"] = E.order_ticket(q, sg.get("premium"), _pl.get("stop_pct") or 30,
                                                     (_pl.get("tp1_pct") or 35) / 100.0, _t2f)
                        save_signals()
                    r = _live_reco(sg.get("premium"), q["mid"], q.get("ask"), q.get("spread_pct"))
                    if r:
                        _ind = _tape_cached(conn, sg["ticker"])                 # fresh tape for the timing verdict
                        _ss = sg.get("stated_stop") or {}
                        _us = _ss.get("value") if _ss.get("kind") == "underlying" else None
                        r["entry"] = entry_timing(sg, _ind, r, u_stop=_us, spot=(_ind or {}).get("price"))
                        # ONE VOICE: ping only when the FULL entry verdict flips to TAKE (price in zone
                        # AND tape pushing AND room to the stop) — a raw price-only "buy zone" ping while
                        # the verdict says WAIT (cheap because it's flat) just teaches you to ignore pings.
                        prev_act = (LIVE_SIG.get(s["id"], {}).get("entry") or {}).get("action")
                        if r["entry"]["action"] == "TAKE" and prev_act != "TAKE":
                            tk = s.get("ticket") or {}
                            leg = f'{sg["ticker"]} {sg["strike"]}{sg["type"]}'
                            _alert(f"🟢 TAKE NOW — {s['provider']}: {leg} — {r['entry']['head']} · "
                                   f"enter ~${q.get('ask') or r['mid']}, stop ${tk.get('stop', '?')} · "
                                   f"T1 ${tk.get('t1','?')} / T2 ${tk.get('t2','?')}", "Signal Desk — 🟢 TAKE")
                        LIVE_SIG[s["id"]] = r
                    conn.sleep(1)
                except Exception:
                    continue
            for k in list(LIVE_SIG):                          # drop stale ids no longer in the feed
                if k not in {s["id"] for s in SIGNALS}:
                    LIVE_SIG.pop(k, None)
            # SHADOW TRACK skipped signals over their FULL LIFE: the disciplined would-be result (first
            # T1/stop hit) AND the ultimate peak/trough (what we truly missed or dodged), until expiry.
            shadows = [s for s in SIGNALS if s.get("shadow") and s.get("status") not in ("TAKEN", "CLOSED")
                       and s["shadow"].get("state") != "ended" and (s.get("sig") or {}).get("premium")]
            today = datetime.now(ET).strftime("%Y%m%d")
            now_utc = datetime.now(timezone.utc).isoformat()
            dirty = False
            for s in shadows[-20:]:
                try:
                    sh, sg = s["shadow"], s["sig"]
                    if sh.get("state") in ("WIN", "LOSS"):        # migrate old resolved -> keep tracking the full life
                        sh["result"], sh["state"] = sh.get("result") or sh["state"], "live"
                    exp = sh.get("expiry") or _resolve_expiry(sg) or (s.get("opt") or {}).get("expiry")
                    if exp and exp < today:                       # contract expired -> END its life
                        sh["state"], sh["ended_utc"] = "ended", now_utc
                        if not sh.get("result"):
                            sh["result"] = ("WIN" if sh.get("peak", 0) >= sh["t1_pct"]
                                            else "LOSS" if sh.get("trough", 0) <= -sh["stop_pct"] else "SCRATCH")
                        dirty = True
                        continue
                    q = MK.option_quote(sg["ticker"], str(sg["strike"]).split(".")[0], sg["type"], expiry=exp, ib=conn)
                    if not q or q.get("error") or q.get("mid") is None:
                        continue
                    pct = round((q["mid"] / sh["ref"] - 1) * 100, 1)
                    sh["now"] = pct
                    sh["peak"] = round(max(sh.get("peak", 0), pct), 1)
                    sh["trough"] = round(min(sh.get("trough", 0), pct), 1)
                    _path_tick(s["id"], "shadow", q, sg["ticker"], sh["ref"], sh, sh.get("start_utc"), conn)
                    if not sh.get("result"):                      # disciplined result = whichever we'd hit FIRST
                        if pct <= -sh["stop_pct"]:
                            sh["result"], sh["result_pct"] = "LOSS", -sh["stop_pct"]
                        elif pct >= sh["t1_pct"]:
                            sh["result"], sh["result_pct"] = "WIN", sh["t1_pct"]
                    if q["mid"] <= 0.02:                          # decayed to ~worthless -> END its life
                        sh["state"], sh["ended_utc"] = "ended", now_utc
                        if not sh.get("result"):
                            sh["result"] = "LOSS" if sh.get("trough", 0) <= -sh["stop_pct"] else "SCRATCH"
                    dirty = True
                    conn.sleep(1)
                except Exception:
                    continue
            if dirty:
                save_signals()
            if DB:
                DB.sync_outcomes(SIGNALS)             # capture shadow resolutions + realized closes
            conn.sleep(20)
        except Exception:
            try: conn.disconnect()
            except Exception: pass
            conn = None
            import time; time.sleep(10)


def ustop_loop():
    """FAST underlying-price poll, independent of the heavy option monitor. Every ~6s it snapshots the
    live spot for each open position carrying a provider underlying-stop (u_stop), updates the card's
    spot/direction, and fires the CUT alert the instant spot crosses — so the stop is never blind for a
    full slow monitor cycle. Its own IBKR client so it can't be starved by the option-quote loop."""
    import market as MK, time
    conn = None
    while True:
        try:
            positions = [p for p in M.open_positions() if p.get("u_stop") and p.get("ticker")]
            reentries = [s for s in SIGNALS if (s.get("reentry") or {}).get("armed")]
            if not positions and not reentries:
                time.sleep(6); continue
            if conn is None:
                conn = MK.connect(client_id=81)
                try: conn.reqMarketDataType(int(MK.env("IBKR_MARKET_DATA_TYPE", "2")))
                except Exception: pass
            # --- LIVE RE-ENTRY ANALYSIS: cards the user armed for a re-entry read (closed/exited too) ---
            for s in reentries:
                try:
                    re, sg = s["reentry"], (s.get("sig") or {})
                    tkr = re.get("ticker") or sg.get("ticker")
                    cp = re.get("type") or sg.get("type") or "C"
                    exp = re.get("expiry") or _resolve_expiry(sg) or (s.get("opt") or {}).get("expiry")
                    ind = _tape_cached(conn, tkr)
                    if re.pop("roll_requested", False):                # user asked for a fresh strike
                        _td = abs((s.get("opt") or {}).get("delta") or 0.45) or 0.45
                        _roll = _roll_strike(tkr, cp, exp, (ind or {}).get("price"), conn, target_delta=_td)
                        if _roll:
                            re["rolled"] = {"from": sg.get("strike"), "to": _roll["strike"]}
                            re["strike"] = _roll["strike"]; re["ref_exit"] = None; re.pop("analysis", None)
                    strike = str(re.get("strike") or sg.get("strike")).split(".")[0]
                    q = MK.option_quote(tkr, strike, cp, expiry=exp, ib=conn)
                    re["analysis"] = reentry_read(tkr, strike, cp, exp, q, ind,
                                                  ref_exit=re.get("ref_exit"), u_stop=re.get("u_stop"),
                                                  orig_premium=sg.get("premium"), rolled=re.get("rolled"))
                    re["updated"] = datetime.now(ET).isoformat()
                    conn.sleep(1)
                except Exception:
                    continue
            for tk in {p["ticker"] for p in positions}:
                px = MK.spot_quote(tk, ib=conn)
                if not px:
                    continue
                prev = SPOT_CACHE.get(tk, (0, None))[1]
                SPOT_CACHE[tk] = (time.time(), px)
                for p in positions:
                    if p.get("ticker") != tk:
                        continue
                    st = MONITOR.setdefault(p["id"], {"fired": set()})
                    if prev is not None and abs(px - prev) > 1e-9:
                        st["spot_dir"] = "up" if px > prev else "down"
                    if not st.get("entry_spot"):
                        st["entry_spot"] = px                 # first observed spot ≈ entry (until slow loop refines)
                    st["spot"] = px
                    _check_ustop(p, st)                       # near-real-time provider-stop cut alert
            time.sleep(6)
        except Exception:
            try: conn.disconnect()
            except Exception: pass
            conn = None
            import time as _t; _t.sleep(6)


def _apply_entry_fill(prov, text, c, data, res):
    """A firm fill ("IN AT 0.93") upgrades a WATCH card to an entered position at the REAL fill price.
    Links to the watched setup by ticker+strike, else ticker named in the fill, else the nearest watched
    premium among the provider's recent watches. Returns None if there's no watch to upgrade (so ingest
    can fall through and card it as a fresh entry instead)."""
    price = c.get("price")
    now = datetime.now(ET)
    watches = [s for s in reversed(SIGNALS[-150:])
               if s.get("provider") == prov and s.get("status") == "WATCHING"
               and (now - datetime.fromisoformat(s["iso"])).total_seconds() < 86400]
    if not watches:
        return None
    named = {k for k in M.KNOWN if re.search(rf"\b{re.escape(k)}\b", text, re.I)}
    fsig = res.get("sig") if res else None
    if fsig and fsig.get("ticker"):                       # fill fully specifies a contract
        cand = [w for w in watches if w["sig"]["ticker"] == fsig["ticker"]
                and str(w["sig"]["strike"]).split(".")[0] == str(fsig["strike"]).split(".")[0]]
        link = "ticker+strike"
        if not cand:
            cand = [w for w in watches if w["sig"]["ticker"] == fsig["ticker"]]; link = "ticker"
        if not cand:
            return None                                  # a contract with no watch -> fresh entry, don't hijack
    elif named:                                          # fill names ticker(s) — only upgrade a NAMED watch
        cand = [w for w in watches if w["sig"]["ticker"] in named]
        link = "ticker"
        if not cand:
            return None                                  # named tickers, none watched -> not a watch upgrade
    else:                                                # no ticker context at all ("IN AT 0.93") -> recent watch
        cand, link = watches, "recent_watch"
    provisional = len(cand) > 1                           # 2+ candidate watches -> genuinely ambiguous
    if provisional and price is not None:                 # best guess = nearest watched premium to the fill
        cand = sorted(cand, key=lambda w: abs((w["sig"].get("premium") or 9e9) - price))
        link += "+nearest_premium"
    w = cand[0]
    _lbl = lambda x: f'{x["sig"]["ticker"]} {x["sig"]["strike"]}{x["sig"]["type"]}'
    alt = [_lbl(x) for x in cand[1:]]
    # the OTHER candidate watches remain WATCHING and can be switched to (one-tap confirm)
    candidates = [{"id": x["id"], "label": _lbl(x), "watched_at": x["sig"].get("premium")} for x in cand[1:]]
    old = w["sig"].get("premium")
    w["sig"]["watch"] = False
    w["watch"] = False
    if price is not None:
        w["sig"]["premium"] = price                      # the REAL fill replaces the watched level
    w["status"] = "NEW"                                   # now actionable — provider is IN
    w["entered_from_watch"] = {"watched_at": old, "entered_at": price, "link": link,
                               "provisional": provisional, "candidates": candidates,
                               "alternatives": alt, "raw": text[:160],
                               "ts_utc": now.astimezone(timezone.utc).isoformat()}
    try:                                                  # re-size off the REAL fill (watch was sized off the level)
        _pv = w["sig"].get("provider"); _sc = w.get("score") or 50
        _conv = w.get("conviction") or E.conviction_factor(_sc - E.trust_of(_pv))
        _tech = w.get("tech") or {}
        _rp = round(E.size_pct(_sc, w["sig"].get("lotto"), E.provider_size_mult(_pv),
                               w["sig"].get("premium"), base_trust=E.trust_of(_pv)) * E.THROTTLE, 3)
        w["plan"] = E.plan_data(w["sig"], _rp, conviction=_conv, q=w.get("opt"),
                                ind=_tech.get("ind"), rv=_tech.get("rv"))
        _ov = E.provider_manual_override(_pv)
        if E.provider_size_mult(_pv) == 0 and _ov and not w["plan"].get("ok") and w["sig"].get("premium"):
            w["plan"] = E.plan_data(w["sig"], conviction=_conv, q=w.get("opt"), ind=_tech.get("ind"),
                                    rv=_tech.get("rv"), force_contracts=_ov)
            w["plan"]["manual"] = True
    except Exception:
        pass
    save_signals()
    _archive_signal(prov, w)                              # record the watch->entered transition
    return {"outcome": "ENTERED", "card": w["id"], "ticker": w["sig"]["ticker"],
            "leg": f'{w["sig"]["strike"]}{w["sig"]["type"]}', "watched_at": old,
            "entered_at": price, "link": link, "alternatives": alt, "provisional": provisional}


_MISSED_ALERTED = set()   # texts already flagged as a possible missed signal (dedup the ping)


def _maybe_missed_entry(text, c):
    """A message we FAILED to card that still smells like an entry — a ticker-ish token + a strike-like
    number + a price decimal, with no exit/stat verb. Surfacing these means a NEW provider format can
    never silently vanish again: it pings for review so Chip can teach the desk the shape."""
    low = (text or "").lower()
    if c.get("type") in ("TRIM", "SCALE_OUT", "FULL_EXIT", "CUT", "MOVE_STOP", "PROFIT_UPDATE"):
        return False
    if re.search(r"\b(out|off|sold|selling|trim|cut|close|closing|stop|recap|review|watchlist|outlook)\b", low):
        return False
    if "%" in text:                                          # profit/stat chatter, not an entry
        return False
    has_price = bool(re.search(r"(?<![\d.])(?:\.\d{1,2}|\d{1,4}\.\d{1,2})\b", text or ""))
    has_strike = bool(re.search(r"(?<![\d.])\d{2,5}\b", text or ""))
    has_alpha = bool(re.search(r"[A-Za-z]{2,5}", text or ""))
    return has_price and has_strike and has_alpha


def ingest(data):
    text = (data.get("text") or "").strip()
    # Channel map is authoritative (one room = one product): a single author (e.g. Prince) runs
    # BOTH a main room and the Small Account Challenge, so author-matching alone folds them together.
    # Author detection is only the fallback for channels we haven't mapped.
    prov = CHMAP.get(data.get("channel")) or E.detect_provider(data.get("author")) or data.get("provider")
    _inlog(data.get("channel"), prov, text, "RECV")   # ground-truth: this message reached the server
    if prov and E.provider_disabled(prov):            # user ELIMINATED this provider — drop it entirely
        _inlog(data.get("channel"), prov, text, "DISABLED")
        return {"disabled": prov}
    # PROVIDER EDIT: they fixed a callout (e.g. 7/13 -> 7/15). Drop the stale un-taken card so the
    # corrected text re-cards fresh (re-parsed + re-priced). Never touch a card you've already TAKEN.
    if data.get("edited") and data.get("msg_id"):
        old = next((s for s in SIGNALS if s.get("msg_id") == data.get("msg_id")), None)
        if old and old.get("status") in ("NEW", "WATCHING"):
            SIGNALS.remove(old)
            save_signals()
            _inlog(data.get("channel"), prov, text, "EDIT→REPRICE")
    refresh_account()                       # COMPOUND: size off current sleeve equity

    # Parse a fresh entry FIRST: a message that carries a full ticker+strike+premium (e.g. Mike's
    # sloppy "mu 1000c 2.75 taking one") is a BUY, and must not be second-guessed by the mgmt LLM —
    # "taking one" tricked it into TRIM, silently dropping the card.
    res = E.analyze(text, SEG, provider=prov)
    entry_priced = bool(res) and (res.get("sig") or {}).get("premium") is not None

    # management follow-up? (has an action verb) -> attach to an open position, not a new card
    c = M.classify(text)
    if not entry_priced and c["type"] == "NOISE" and prov and _looks_mgmt(text) and M.open_positions():
        c2 = M.classify_llm(text)                     # LLM fallback for messy messages
        if c2 and c2.get("type") not in (None, "NOISE"):
            c = c2

    # A FRESH ENTRY wins over the management classifier: providers put stops ("s/l 1.20") IN their
    # entries, which would otherwise look like MOVE_STOP. Only a sell/exit verb marks a real update.
    fresh_entry = bool(res) and c["type"] not in ("TRIM", "SCALE_OUT", "FULL_EXIT", "CUT")

    # ---- FILL CONFIRMATION: "IN AT 0.93" upgrades a WATCH to entered at the REAL fill price.
    #      Try this first so a fill doesn't get carded as a brand-new signal or dropped as noise. ----
    if c["type"] == "ENTRY_FILL":
        r = _apply_entry_fill(prov, text, c, data, res)
        if r:
            _inlog(data.get("channel"), prov, text, "FILL→" + (r.get("outcome") or "?"))
            if r.get("watched_at") is not None:
                if r.get("provisional"):
                    _alert(f"⚠ {prov}: assumed entered {r['ticker']} {r['leg']} @ ${r['entered_at']} "
                           f"— AMBIGUOUS (alt: {', '.join(r['alternatives'])}). Confirm on the desk.",
                           "Signal Desk — Confirm entry")
                else:
                    _alert(f"✅ {prov}: entered {r['ticker']} {r['leg']} @ ${r['entered_at']} "
                           f"(watched ${r['watched_at']})", "Signal Desk — Entered")
            return r
        # else: no watch to upgrade -> fall through (card as a fresh entry if it has a strike)

    # ---- CROSS-DAY REPLY-TAGGING: reply_to carries the parent's FULL TEXT, so parse the
    #      original signal out of it and match by trade_key across days (positions + feed) ----
    reply_to = data.get("reply_to")
    if not fresh_entry and reply_to and c["type"] != "NOISE":
        pkey, psig = M.reply_parent_key(reply_to, prov)
        if pkey:
            kind, target = M.find_card_by_key(pkey, SIGNALS)
            if kind == "pos":
                if not M.reply_already_logged(target["id"], text):
                    M.attach_to_position(target["id"], c, source="reply")
                _inlog(data.get("channel"), prov, text, "REPLY→POS")
                return {"management": c["type"], "linked": target["id"], "via": "reply_text"}
            if kind == "sig":
                M.attach_to_signal_card(target, c)     # parent seen but not taken -> stamp the card
                save_signals()
                _inlog(data.get("channel"), prov, text, "REPLY→SIG")
                return {"management": c["type"], "sig_update": target["id"], "via": "reply_text"}
            tp = M.open_tracked_from_reply(prov, psig, c, reply_to)   # never saw entry -> tracked shell
            _inlog(data.get("channel"), prov, text, "REPLY→TRACK")
            return {"management": c["type"], "tracked": tp["id"], "via": "reply_text"}

    if not fresh_entry and c["type"] in ("TRIM", "SCALE_OUT", "FULL_EXIT", "CUT", "MOVE_STOP", "PROFIT_UPDATE"):
        _inlog(data.get("channel"), prov, text, "MGMT")
        r = M.link_and_attach({"text": text, "provider": prov, "reply_to": data.get("reply_to"), "classified": c})
        if r and r.get("position"):
            touch_source(data.get("channel") or prov or "unknown", prov)
            if c["type"] in ("CUT", "FULL_EXIT"):     # urgent -> webhook
                _alert(f"⚠ {prov}: {c['type'].replace('_',' ')} on {c.get('ticker') or 'a position'} — \"{c.get('raw','')}\"",
                       "Signal Desk — Manage")
            return {"management": c["type"], "linked": r["position"]}
        if r and r.get("unlinked"):                   # ambiguous -> disambiguation prompt
            pid = datetime.now(ET).strftime("%H%M%S%f")
            PENDING.append({"id": pid, "provider": prov, "ts": datetime.now(ET).isoformat(),
                            "classified": r["classified"], "candidates": r["candidates"]})
            return {"management": c["type"], "pending": pid}
        if c["type"] != "PROFIT_UPDATE":
            return {"management": c["type"], "linked": None}

    if not res:                                             # res computed above (fresh-entry check)
        if prov and _maybe_missed_entry(text, c) and text not in _MISSED_ALERTED:
            _MISSED_ALERTED.add(text)
            _inlog(data.get("channel"), prov, text, "MISS?")
            _alert(f"⚠ POSSIBLE MISSED SIGNAL from {prov} — Chip couldn't parse:\n\"{text[:170]}\"\n"
                   f"If this is a real entry, tell Chip and he'll teach the desk this format.",
                   "Signal Desk — Possible miss")
            return {"unparsed_possible_entry": True, "provider": prov, "text": text[:200]}
        _inlog(data.get("channel"), prov, text, "REJECT")   # not a parseable signal (or commentary)
        return None
    # DE-DUPE — ONE card per trade (trade_key = provider|ticker|strike|type|expiry).
    now = datetime.now(ET)
    sg = res["sig"]
    key = M.trade_key(prov, sg["ticker"], sg["strike"], sg["type"], None)
    _force = bool(data.get("force"))          # DELIBERATE re-entry -> bypass repost/dedup guards
    #  a) a re-post/edit of an already-OPEN trade (any age) -> log it, keep the one card
    op = None if _force else M.find_open_by_key(key)
    if op:
        M.note_repost(op["id"], text)
        return {"duplicate": op.get("journal_id") or op["id"], "merged": True}
    #  a2) a re-post/recap of a signal you've already TAKEN or CLOSED — same provider + same contract +
    #      same fill price, checked across FULL history (any age). Catches a provider re-posting a swing
    #      signal days later, past the 48h / last-120 dedup windows (the MSFT/HIMS "it's a log, not a new
    #      signal" bug). A GENUINE re-entry fills at a DIFFERENT price, so it still gets its own card.
    if not _force and sg.get("premium") is not None:
        for s in reversed(SIGNALS):
            if s.get("provider") != prov or not s.get("sig") or s.get("status") not in ("TAKEN", "CLOSED"):
                continue
            if M.trade_key(prov, s["sig"]["ticker"], s["sig"]["strike"], s["sig"]["type"], None) != key:
                continue
            _sp = s["sig"].get("premium")
            if _sp is not None and abs(_sp - sg["premium"]) < 1e-9:
                s.setdefault("reposts", 0); s["reposts"] += 1
                _inlog(data.get("channel"), prov, text, "REPOST→SKIP")
                return {"duplicate": s["id"], "repost": s["reposts"], "echo": True}
    #  b) a re-post/bump of an existing card — same Discord message ever (permanent), OR same trade_key
    #     within 4h (providers bump repeatedly), OR the SAME entry text re-posted within 48h, OR an ECHO:
    #     same trade + same fill CORROBORATED by a stale marker. The echo path catches a provider quoting/
    #     recapping a prior-day entry (e.g. "QQQ 722P expiring today, 1.10 fill" re-posted next day) while
    #     still letting a GENUINE next-day re-entry through — that would fill at a new price or set a new
    #     target. Whitespace is normalized so "\n" vs " " still matches; 48h spans an overnight echo.
    _norm = lambda t: re.sub(r"\s+", " ", (t or "")).strip().lower()
    _target = lambda t: (lambda m: float(m.group(1)) if m else None)(
        re.search(r"(?:price\s*target|target|\bpt)\s*:?\s*\$?(\d{2,5}(?:\.\d{1,2})?)", (t or ""), re.I))
    ntext = _norm(text)[:120]
    _low = text.lower()
    is_reply = bool(data.get("reply_to"))                          # a Discord reply/quote references an old post
    is_recap = bool(re.search(r"(?:could|should|would)'?ve|(?:could|should|would)\s+have|"
                              r"\byesterday\b|\brecap\b|in hindsight|looking back|last time|that trade",
                              _low))
    ctgt = _target(text)
    mid = data.get("msg_id")
    for s in ([] if _force else reversed(SIGNALS[-120:])):    # force -> skip the whole dedup sweep
        sk = M.trade_key(s.get("provider"), s["sig"]["ticker"], s["sig"]["strike"], s["sig"]["type"], None)
        age = (now - datetime.fromisoformat(s["iso"])).total_seconds()
        same_msg = bool(mid) and s.get("msg_id") == mid
        same_raw = s.get("provider") == prov and _norm(s.get("raw"))[:120] == ntext
        # same trade + same fill premium, but ONLY treated as an echo when a stale marker corroborates it:
        # same stated price target, a reply/quote, or recap language. Fill alone is NOT enough — a real
        # same-price re-entry with a NEW target and a clean entry post still gets its own card.
        _sp, _cp = s["sig"].get("premium"), sg.get("premium")
        same_fill = sk == key and _sp is not None and _cp is not None and abs(_sp - _cp) < 1e-9
        stgt = _target(s.get("raw"))
        same_target = ctgt is not None and stgt is not None and abs(ctgt - stgt) < 0.01
        echo_fill = same_fill and (same_target or is_reply or is_recap)
        if same_msg or (sk == key and age < 14400) or (same_raw and age < 172800) \
                or (echo_fill and age < 172800):
            s.setdefault("reposts", 0)
            s["reposts"] += 1
            echo = bool((same_raw or echo_fill) and age >= 43200)
            return {"duplicate": s["id"], "repost": s["reposts"], "echo": echo}
    # enrich with LIVE IBKR technicals + option Greeks, and VERIFY the contract actually exists
    res["verified"] = None                     # None=unchecked, True=real contract, False=misparse
    res["verify_note"] = ""
    if ENRICH:
        try:
            import market
            _exp = _resolve_expiry(res["sig"])            # price the SIGNAL'S expiry (e.g. 7/9), not nearest/0DTE
            tech = market.context(res["sig"]["ticker"], res["sig"]["type"],
                                  strike=res["sig"]["strike"], expiry=_exp)
            if not tech.get("ok"):
                err = (tech.get("error") or "").lower()
                if "security definition" in err or "unknown contract" in err or "no valid" in err:
                    res["verified"] = False
                    res["verify_note"] = f"IBKR: no such underlying '{res['sig']['ticker']}' — likely misparse"
            else:
                refresh_account(tech.get("equity"))   # apply chosen compounding source
                res["score"] = max(0, min(100, res["score"] + tech["adj"]))
                s = res["score"]
                res["tier_key"] = "HIGH" if s >= 75 else "MEDIUM" if s >= 60 else "LOW" if s >= 45 else "SKIP"
                res["reasons"] = res["reasons"] + [f"tape {tech['label']} ({tech['adj']:+d})"]
                if tech.get("opt_label"):
                    res["reasons"] = res["reasons"] + [f"{tech['opt_label']} ({tech.get('opt_adj', 0):+d})"]
                res["tech"] = tech
                opt = tech.get("opt") or {}
                res["opt"] = opt                       # live option quote for the card
                oerr = (opt.get("error") or "").lower()
                if opt and not opt.get("error") and opt.get("mid") is not None:
                    res["verified"] = True
                    res["verify_note"] = f"IBKR contract confirmed — mid ${opt.get('mid')}"
                elif "security definition" in oerr or "no valid expiries" in oerr:
                    res["verified"] = False
                    res["verify_note"] = (f"IBKR: {res['sig']['ticker']} {res['sig']['strike']}"
                                          f"{res['sig']['type']} is not a real strike — misparse")
                # COST re-weight (#2): fold live mechanical costs the base score misses — chasing the
                # provider's entry, theta decay, time-of-day. Spread + delta are already in option_score.
                _nowmin = max(0, (datetime.now(ET).hour * 60 + datetime.now(ET).minute) - 570)  # min since 9:30 ET
                _cadj, _creasons = E.cost_adjust(opt, res["sig"], now_min=_nowmin)
                if _cadj:
                    res["score"] = max(0, min(100, res["score"] + _cadj))
                    s = res["score"]
                    res["tier_key"] = "HIGH" if s >= 75 else "MEDIUM" if s >= 60 else "LOW" if s >= 45 else "SKIP"
                    res["reasons"] = res["reasons"] + _creasons
                # HARD JUNK GATE — block decay traps / far-OTM lottos / slippage traps outright (stricter
                # for low-trust providers whose signals are proven money-losers). This is the strict catch.
                _chase = (opt.get("mid") / res["sig"].get("premium") - 1) * 100 if (opt.get("mid") and res["sig"].get("premium")) else 0
                _j = E.junk_setup(opt, res["sig"], provider=res["sig"].get("provider"), chase_pct=_chase)
                if _j["block"] or _j["warn"]:
                    res["junk"] = {**_j, "provider": res["sig"].get("provider")}
                # re-size with the SAME provider multiplier + conviction as analyze() — else the
                # tape-adjusted card silently reverts to full-trust sizing (the leak).
                _pv = res["sig"].get("provider")
                _conv = E.conviction_factor(s - E.trust_of(_pv))
                res["conviction"] = _conv
                _rp = round(E.size_pct(s, res["sig"]["lotto"], E.provider_size_mult(_pv),
                                       res["sig"].get("premium"), base_trust=E.trust_of(_pv)) * E.THROTTLE
                            * E.provider_edge_mult(_pv), 3)   # calibration-gated tilt (bleeders shrink)
                res["plan"] = E.plan_data(res["sig"], _rp, conviction=_conv, q=tech.get("opt"),
                                          ind=tech.get("ind"), rv=tech.get("rv"), now_min=_nowmin)
                _ov = E.provider_manual_override(_pv)              # paper provider -> 1-lot discretionary plan
                if E.provider_size_mult(_pv) == 0 and _ov and not res["plan"].get("ok") and res["sig"].get("premium"):
                    res["plan"] = E.plan_data(res["sig"], conviction=_conv, q=tech.get("opt"),
                                              ind=tech.get("ind"), rv=tech.get("rv"), now_min=_nowmin,
                                              force_contracts=_ov)
                    res["plan"]["manual"] = True
                # ORDER TICKET: the fillable limit/stop/targets off the LIVE market, for manual broker entry
                _o = res.get("opt")
                if _o and not _o.get("error") and _o.get("mid") is not None:
                    _pl = res.get("plan") or {}
                    _e = _pl.get("entry") or res["sig"].get("premium") or 0
                    _t2f = (_pl["tp2"] / _e - 1) if (_e and _pl.get("tp2")) else 0.70
                    _t3f = (_pl["tp3"] / _e - 1) if (_e and _pl.get("tp3")) else 1.10
                    _sf = (_pl.get("sold", 0) / _pl["contracts"]) if _pl.get("contracts") else 0.667
                    res["ticket"] = E.order_ticket(_o, res["sig"].get("premium"),
                                                   _pl.get("stop_pct") or 30,
                                                   (_pl.get("tp1_pct") or 35) / 100.0, _t2f, _sf, _t3f)
                # SETUP GRADE (#3) — PURE execution quality: liquidity + buy-zone + risk-clarity (delta),
                # NOT R:R (that rewards far-OTM traps). Awards MORE size to cleanly-executable setups.
                _z = (res["sig"].get("dte") == 0) or (res["sig"].get("expiry") == "0DTE")
                gr = E.setup_grade(res.get("ticket"), res.get("tier_key"), res["sig"].get("lotto"),
                                   (res.get("ticket") or {}).get("chasing_pct") or 0,
                                   delta=(res.get("opt") or {}).get("delta"), zero_dte=_z)
                res["grade"] = gr
                if res.get("plan", {}).get("ok") and not res["plan"].get("manual") and gr["mult"] != 1.0:
                    res["plan"] = E.plan_data(res["sig"], round(_rp * gr["mult"], 3), conviction=_conv,
                                              q=tech.get("opt"), ind=tech.get("ind"), rv=tech.get("rv"),
                                              now_min=_nowmin)
                # JMT rule: FLOOR of 3 contracts, scale UP to 3% risk on cheap options. contracts =
                # max(3, contracts-at-3%). On pricey options the 3-contract floor can exceed 3% (user's call).
                _pl = res.get("plan") or {}
                if _pv == "jmt" and _pl.get("ok"):
                    _p = res["sig"].get("premium") or (res.get("opt") or {}).get("mid") or 0
                    if _p:
                        _by3 = int((0.03 * E.ACCOUNT) / (_p * 100 * 0.5))   # contracts allowed by 3% (to −50% stop)
                        _n = max(3, _by3)                                    # 3 floor, scale up when cheap
                        _pl["contracts"] = _n
                        _pl["capital"] = round(_n * _p * 100)
                        _pl["risk"] = round(_n * _p * 100 * 0.5)
                        _pl["risk_pct"] = round(_pl["risk"] / E.ACCOUNT * 100, 2)
                        _pl["jmt_sized"] = True
                        _pl["over_3pct"] = _pl["risk_pct"] > 3.05             # flag pricey-option floor
        except Exception:
            pass
    now = datetime.now(ET)
    sid = now.strftime("%H%M%S") + "-" + res["sig"]["ticker"] + res["sig"]["strike"]
    entry = {"id": sid, "ts": now.strftime("%H:%M:%S"), "iso": now.isoformat(),
             "ts_utc": now.astimezone(timezone.utc).isoformat(),
             "provider": prov or "unknown",
             "status": "WATCHING" if res.get("watch") else "NEW",   # provider eyeing it, not filled yet
             "msg_id": data.get("msg_id"), "raw": text[:200], **res}
    # Price-plausibility flag: if the live contract price is an order of magnitude off the provider's
    # stated premium, the signal is a misparse (wrong strike/expiry, or a number that isn't a premium —
    # e.g. "META 375C @ 2.9" when META is $674 and that call is worth $298). Warn; don't trust the ticket.
    _om = (entry.get("opt") or {}).get("mid")
    _pm = (entry.get("sig") or {}).get("premium")
    if _om and _pm and (_om > _pm * 4 or _om * 4 < _pm):
        entry["price_mismatch"] = {"stated": _pm, "market": _om}
    # AUTO-SHADOW every carded signal (ALL providers) at ingest so provider edge is COMPLETE — a signal
    # you neither take nor dismiss still resolves to a would-be outcome, killing selection bias. Taken ->
    # becomes a real position (drops out of the shadow filter); anything else -> shadow-tracked. Junk isn't.
    if not (entry.get("junk") or {}).get("block") and (entry.get("sig") or {}).get("premium"):
        _init_shadow(entry)
    SIGNALS.append(entry)
    save_signals()
    _inlog(data.get("channel"), prov, text, "CARD")
    _archive_signal(prov, entry)                    # permanent structured archive of every signal
    if DB:
        DB.record_signal(entry)                     # -> SQLite warehouse (features as-of decision)
    if entry.get("paper") and prov:                 # B4: log paper-provider entries for graduation
        _log_paper_entry(prov, sid, entry)
    touch_source(data.get("channel") or prov or "unknown", prov,
                 f"{res['sig']['ticker']} {res['sig']['strike']}{res['sig']['type']}", data.get("name"))
    _signal_alert(entry)                            # push the FULL info of EVERY new card to Discord
    # terminal echo
    print(f"\n⚡ {entry['ts']} {prov or '?'}  {res['sig']['ticker']} {res['sig']['strike']}"
          f"{res['sig']['type']}  conf {res['score']}/100 {res['tier_key']}", flush=True)
    return entry


BROKER_ORDERS = []   # {ts, ticker, action} — orders sent through the gateway today (for tilt/frequency rules)


def broker_order(data):
    """Route a Take through the disciplined gateway: build the rule context, gate it, and only if it
    passes does it reach tastytrade. dry_run=True previews (gate + broker validation, no fill)."""
    if not BROKER:
        return {"error": "tastytrade not connected"}
    entry = next((s for s in SIGNALS if s["id"] == data.get("id")), None)
    if not entry:
        return {"error": "unknown card"}
    sig = entry.get("sig") or {}
    try:
        qty = max(1, int(data.get("qty") or 1))
        price = float(data.get("price"))
    except Exception:
        return {"error": "need qty and price"}
    action = (data.get("side") or "BTO").upper()
    exp = _resolve_expiry(sig) or (entry.get("opt") or {}).get("expiry")
    if not exp:
        return {"error": "no expiry resolved for this contract"}
    today = datetime.now(ET).date().isoformat()
    todays = [o for o in BROKER_ORDERS if o["ts"][:10] == today]
    try:
        byday = (ST.performance().get("by_day") or [])
        day_real = next((d["total"] for d in byday if d["date"] == today), 0.0)
    except Exception:
        day_real = 0.0
    junk = entry.get("junk") or {}
    _jmt = entry.get("provider") == "jmt"
    # JMT: 3-contract FLOOR, scale to 3% on cheap, 5% ceiling. Prince/Small (+everyone else): hard 1%.
    _cap = 5.0 if _jmt else 1.0
    # risk to the STATED stop (JMT & Prince both post stops) — not full premium; fall back to -50%
    # (JMT) or full premium (no stop = gap-risk) only when the card has no stop.
    _sp = (entry.get("plan") or {}).get("stop_pct") or (entry.get("ticket") or {}).get("stop_pct")
    _stop_frac = (_sp / 100.0) if _sp else (0.5 if _jmt else 1.0)
    _risk = round(qty * price * 100 * _stop_frac, 2)
    ctx = BROKER.OrderCtx(
        ticker=sig.get("ticker"), risk_usd=_risk, account_equity=float(E.ACCOUNT),
        day_realized_usd=float(day_real), trades_today=len(todays),
        reentries_today=sum(1 for o in todays if o["ticker"] == sig.get("ticker")),
        junk_block=bool(junk.get("block")), junk_reasons=junk.get("reasons") or [],
        max_risk_pct=_cap, qty=qty, max_contracts=0)
    dry = data.get("dry_run", True)
    res = BROKER.submit_order(ctx, (sig.get("ticker"), exp, sig.get("type"), sig.get("strike")),
                              action, qty, price, dry_run=dry)
    if res.get("placed"):
        BROKER_ORDERS.append({"ts": datetime.now(ET).isoformat(), "ticker": sig.get("ticker"), "action": action})
        _inlog("broker", entry.get("provider"), f"{sig.get('ticker')} {sig.get('strike')}{sig.get('type')} "
               f"{action} x{qty} @ {price}", "TT-SENT")
    return res


def act(data):
    if data.get("action") == "withdraw":       # bank profit to reserve (user-recorded)
        amt = float(data.get("amount") or 0)
        if amt > 0:
            EQ.add_withdrawal(amt)
            refresh_account()
        return {"ok": True, "withdrawn": amt, "sleeve": SLEEVE}
    if data.get("action") == "override_daystop":   # deliberately trade through today's soft day-stop
        global DAY_OVERRIDE_DATE
        DAY_OVERRIDE_DATE = datetime.now(ET).date().isoformat() if data.get("on") else None
        _save_override()
        return {"ok": True, "override": _day_override_active()}

    sid, action = data.get("id"), data.get("action")
    entry = next((s for s in SIGNALS if s["id"] == sid), None)
    if not entry:
        return {"error": "unknown id"}
    if action == "enter":                      # promote a WATCH to an actionable entry at the stated fill
        try:
            price = float(data.get("price")) if data.get("price") not in (None, "") else None
        except Exception:
            price = None
        old = entry["sig"].get("premium")
        entry["sig"]["watch"] = False
        entry["watch"] = False
        if price is not None:
            entry["sig"]["premium"] = price
        entry["status"] = "NEW"
        entry["entered_from_watch"] = {"watched_at": old, "entered_at": price, "link": "manual",
                                       "ts_utc": datetime.now(timezone.utc).isoformat()}
        save_signals()
        _archive_signal(entry["provider"], entry)
        return {"ok": True, "status": "NEW", "entered_at": price, "watched_at": old}
    if action == "reentry_open":               # arm a LIVE re-entry analysis on this card (works on closed)
        sg = entry.get("sig") or {}
        _ss = sg.get("stated_stop") or {}
        entry["reentry"] = {"armed": True,
                            "ticker": data.get("ticker") or sg.get("ticker"),
                            "strike": data.get("strike") if data.get("strike") is not None else sg.get("strike"),
                            "type": data.get("type") or sg.get("type") or "C",
                            "expiry": data.get("expiry") or _resolve_expiry(sg) or (entry.get("opt") or {}).get("expiry"),
                            "ref_exit": data.get("ref_exit"),
                            "u_stop": _ss.get("value") if _ss.get("kind") == "underlying" else None,
                            "opened": datetime.now(timezone.utc).isoformat()}
        save_signals()
        return {"ok": True, "reentry": "armed"}
    if action == "reentry_strike":             # dial the strike/expiry being analyzed (re-strike the read)
        re = entry.get("reentry") or {"armed": True}
        if data.get("strike") is not None: re["strike"] = data.get("strike")
        if data.get("type"): re["type"] = data.get("type")
        if data.get("expiry"): re["expiry"] = data.get("expiry")
        re["armed"] = True; re.pop("analysis", None)
        entry["reentry"] = re; save_signals()
        return {"ok": True, "reentry": "restruck", "strike": re.get("strike")}
    if action == "reentry_promote":            # turn the live read into a FRESH actionable entry card
        re = entry.get("reentry") or {}
        a = re.get("analysis") or {}
        if not a or a.get("error"):
            return {"error": "no live analysis yet — open the panel and let it load"}
        exp = re.get("expiry") or ""
        mmdd = f"{int(exp[4:6])}/{int(exp[6:])}" if len(exp) == 8 else ""
        px = a.get("limit") or (a.get("option") or {}).get("mid")          # plan around the pullback-limit
        sl = re.get("u_stop")
        tkr = re.get("ticker") or (entry.get("sig") or {}).get("ticker")    # MUST include the ticker to resolve
        txt = f"{tkr} {re.get('strike')}{re.get('type') or 'C'} {px} {mmdd}" + (f" SL:{sl}" if sl else "")
        now = datetime.now(ET)
        new = ingest({"text": txt, "provider": entry.get("provider"),
                      "msg_id": f"reenter-{entry['id']}-{now.strftime('%H%M%S')}", "force": True})
        nid = (new or {}).get("id")
        if nid:                                                            # success -> tag & close the watch
            nc = next((s for s in SIGNALS if s["id"] == nid), None)
            if nc: nc["reenter_of"] = entry["id"]
            entry.pop("reentry", None)                                     # close the source watch only on success
        save_signals()
        return {"ok": bool(nid), "reentered": bool(nid), "new_id": nid, "text": txt, "result": new}
    if action == "reentry_roll":               # roll to a FRESH near-the-money strike (original ran too far)
        re = entry.get("reentry") or {"armed": True}
        re["armed"] = True; re["roll_requested"] = True; re.pop("analysis", None)
        entry["reentry"] = re; save_signals()
        return {"ok": True, "reentry": "rolling"}
    if action == "reentry_unroll":             # go back to the provider's original strike
        re = entry.get("reentry") or {}
        re.pop("rolled", None); re["strike"] = (entry.get("sig") or {}).get("strike")
        re["ref_exit"] = data.get("ref_exit") or re.get("ref_exit"); re.pop("analysis", None)
        entry["reentry"] = re; save_signals()
        return {"ok": True, "reentry": "unrolled"}
    if action == "reentry_close":              # stop the live re-entry watch
        entry.pop("reentry", None); save_signals()
        return {"ok": True, "reentry": "closed"}
    if action == "confirm_fill":               # user confirms an AMBIGUOUS fill was linked to the right watch
        efw = entry.get("entered_from_watch")
        if efw:
            efw["provisional"] = False
            efw["candidates"] = []
            efw["link"] = (efw.get("link") or "") + "+confirmed"
        save_signals()
        _archive_signal(entry["provider"], entry)
        return {"ok": True, "confirmed": entry["id"]}
    if action == "relink_fill":                # user moves the fill to a DIFFERENT watched contract
        tgt = next((s for s in SIGNALS if s["id"] == data.get("target")), None)
        if not tgt:
            return {"error": "unknown target"}
        efw = entry.get("entered_from_watch") or {}
        price = efw.get("entered_at")
        entry["sig"]["premium"] = efw.get("watched_at")   # revert this card to a WATCH
        entry["sig"]["watch"] = True
        entry["watch"] = True
        entry["status"] = "WATCHING"
        entry.pop("entered_from_watch", None)
        old = tgt["sig"].get("premium")                   # apply the fill to the chosen contract (confirmed)
        tgt["sig"]["watch"] = False
        tgt["watch"] = False
        if price is not None:
            tgt["sig"]["premium"] = price
        tgt["status"] = "NEW"
        tgt["entered_from_watch"] = {"watched_at": old, "entered_at": price, "link": "user-relink",
                                     "provisional": False, "candidates": [],
                                     "alternatives": [f'{entry["sig"]["ticker"]} {entry["sig"]["strike"]}{entry["sig"]["type"]}'],
                                     "ts_utc": datetime.now(timezone.utc).isoformat()}
        save_signals()
        _archive_signal(tgt["provider"], tgt)
        return {"ok": True, "relinked_to": tgt["id"], "entered_at": price}
    if action == "log_trade":                  # record a COMPLETED manual round-trip — bypasses the day-halt
        try:                                   # (already realized, not new risk)
            ein = float(data.get("entry")); exo = float(data.get("exit")); qty = max(1, int(data.get("qty") or 1))
        except Exception:
            return {"error": "need entry, exit, qty"}
        prov = entry["provider"]; sig = entry["sig"]; pl = entry.get("plan") or {}
        M.open_position(prov, sig.get("ticker"), f"{sig.get('strike','')}{sig.get('type','')}",
                        ein, qty, pl.get("stop"), pl.get("tp1"), pl.get("tp2"), journal_id=sid,
                        entry_msg_id=entry.get("msg_id"), conf=entry.get("score"),
                        tape=(entry.get("tech") or {}).get("label"),
                        expiry=(_resolve_expiry(sig) or (entry.get("opt") or {}).get("expiry")))
        _ps = M.load(); _pid = None
        for _pp in _ps:                        # stamp the origin arc, then close fully at your exit
            if _pp.get("journal_id") == sid:
                _pid = _pp["id"]
                _pp["entry_greeks"] = entry.get("opt")
                _pp["origin"] = {"ts": entry.get("iso"), "ts_utc": entry.get("ts_utc"), "raw": entry.get("raw"),
                                 "sig": entry.get("sig"), "score": entry.get("score"), "tier": entry.get("tier_key"),
                                 "plan": entry.get("plan"), "manual_log": True}
                break
        M.save(_ps)
        if _pid:
            M.apply_action(_pid, "CLOSE", price=exo)
        entry["status"] = "CLOSED"; save_signals(); refresh_account()
        if DB:
            DB.record_signal(entry)
            DB.record_decision(sid, "TAKEN", qty=qty, fill_px=ein,
                               ts_utc=datetime.now(timezone.utc).isoformat())
            DB.sync_outcomes(SIGNALS)
        realized = round((exo - ein) * qty * 100, 2)
        _inlog("manual", prov, f"logged {sig.get('ticker')} {sig.get('strike')}{sig.get('type')} "
               f"{ein}->{exo} x{qty} = {realized:+.0f}", "LOGGED")
        return {"ok": True, "logged": sid, "realized": realized, "status": "CLOSED"}
    if action == "take":                       # discipline wall — cannot take past limits
        refresh_account()
        if SLEEVE.get("halted"):
            return {"error": "blocked", "blocked": True,
                    "reasons": [f"−20% drawdown HALT (sleeve ${SLEEVE['equity']:,.0f}, HWM ${SLEEVE['hwm']:,.0f}) — manual review"]}
        killed = V.killed_providers()          # per-provider kill-switch
        if entry["provider"] in killed:
            return {"error": "blocked", "blocked": True,
                    "reasons": [f"{entry['provider']} auto-disabled — last-20 expectancy {killed[entry['provider']]}% (negative)"]}
        ds = D.state()
        if ds["blocked"] and not _day_override_active():   # soft day-stop — user may deliberately override
            return {"error": "blocked", "blocked": True, "reasons": ds["reasons"]}
        if entry.get("paper") and not entry.get("manual_override"):   # paper provider, no override -> blocked
            return {"error": "blocked", "blocked": True,
                    "reasons": [f"{entry['provider']} is PAPER-only until it proves out — tracking, not trading"]}
        if not entry.get("manual_override"):   # per-provider daily-R / concurrent cap (override bypasses it, 1-lot)
            pcap = (ds.get("provider_R") or {}).get(entry["provider"], {})
            if pcap.get("blocked"):
                return {"error": "blocked", "blocked": True,
                        "reasons": [f"{entry['provider']} at its {pcap.get('reason')} — free budget or wait"]}
    prov = entry["provider"]
    if prov not in E.PROVIDERS:
        entry["status"] = action.upper()
        return {"ok": True, "status": entry["status"], "note": "logged in-memory (unknown provider)"}
    jp = T.paths(prov)[1]
    journal = T.read_journal(jp)
    sig, pl = entry["sig"], entry["plan"]
    row = journal.get(sid, {"id": sid, "alert_ts": entry["iso"], "ticker": sig["ticker"],
                            "strike": sig["strike"], "type": sig["type"],
                            "lotto": "1" if sig["lotto"] else "0",
                            "mike_entry": sig.get("premium") or "", "status": "NEW",
                            "my_entry": "", "my_exit": "", "my_return_pct": "", "notes": "live"})
    if action == "take":
        row["my_entry"] = str(data.get("price") or sig.get("premium") or ""); row["status"] = "TAKEN"
        tape = (entry.get("tech") or {}).get("label", "?")
        row["notes"] = (f"live conf={entry['score']} R={(entry.get('plan') or {}).get('risk_R', 1.0)} "
                        f"tape={tape}")
        try:                                    # open a managed position
            qty = int(data.get("qty") or (pl or {}).get("contracts", 1))   # what YOU actually took
            if entry.get("manual_override"):        # discretionary paper-override -> log YOUR real size
                qty = max(1, min(qty, 20))          # respect the qty you entered (20 = fat-finger ceiling)
            # ANCHOR stop/targets to YOUR ACTUAL FILL — not the provider's stated premium (which can
            # differ, or be mis-priced). The % levels come from the plan; applied to what YOU paid.
            _fill = float(row["my_entry"] or 0) or sig.get("premium") or 0
            _sp = (pl or {}).get("stop_pct") or 45
            _t1p = (pl or {}).get("tp1_pct") or 40
            _t2p = (pl or {}).get("tp2_pct") or 75
            _stop = round(_fill * (1 - _sp / 100.0), 2) if _fill else (pl or {}).get("stop")
            _tp1 = round(_fill * (1 + _t1p / 100.0), 2) if _fill else (pl or {}).get("tp1")
            _tp2 = round(_fill * (1 + _t2p / 100.0), 2) if _fill else (pl or {}).get("tp2")
            M.open_position(prov, sig.get("ticker"), f"{sig.get('strike','')}{sig.get('type','')}",
                            _fill, qty, _stop, _tp1, _tp2, journal_id=sid,
                            entry_msg_id=entry.get("msg_id"),
                            conf=entry.get("score"), tape=(entry.get("tech") or {}).get("label"),
                            expiry=(_resolve_expiry(sig) or (entry.get("opt") or {}).get("expiry")))
            # stamp the FULL ORIGIN SIGNAL CARD onto the position -> the closed trade becomes ONE
            # self-contained record of the entire arc (signal + recommendation + Greeks at entry).
            _ps = M.load()
            for _pp in _ps:
                if _pp.get("journal_id") == sid:
                    _ss = (entry.get("sig") or {}).get("stated_stop") or {}
                    _pp["u_stop"] = _ss.get("value") if _ss.get("kind") == "underlying" else None
                    _pp["entry_greeks"] = entry.get("opt")
                    _pp["entry_stop"] = {"stop": (pl or {}).get("stop"), "stop_pct": (pl or {}).get("stop_pct"),
                                         "terms": (pl or {}).get("stop_terms"), "dynamic": (pl or {}).get("stop_dynamic")}
                    _pp["origin"] = {"ts": entry.get("iso"), "ts_utc": entry.get("ts_utc"),
                                     "raw": entry.get("raw"), "sig": entry.get("sig"),
                                     "score": entry.get("score"), "tier": entry.get("tier_key"),
                                     "conviction": entry.get("conviction"), "size_mult": entry.get("size_mult"),
                                     "reasons": entry.get("reasons"), "verified": entry.get("verified"),
                                     "recommendation": (entry.get("behavior") or {}).get("recommendation"),
                                     "plan": entry.get("plan"), "cosign": entry.get("cosign")}
                    break
            M.save(_ps)
        except Exception:
            pass
    elif action == "close":
        px = data.get("price")
        if px and row.get("my_entry"):
            row["my_return_pct"] = f"{(float(px)/float(row['my_entry'])-1)*100:.1f}"; row["my_exit"] = str(px)
        elif data.get("pct") is not None:
            row["my_return_pct"] = f"{float(data['pct']):.1f}"
        row["status"] = "CLOSED"
    elif action == "skip":
        row["status"] = "SKIPPED"
        _init_shadow(entry)                        # track what we'd have made/lost had we taken it
    journal[sid] = row
    T.write_journal(jp, journal)
    entry["status"] = row["status"]
    save_signals()
    if DB:                                          # warehouse the decision + refresh features as-of now
        DB.record_signal(entry)
        _act = {"TAKEN": "TAKEN", "SKIPPED": "SKIPPED", "CLOSED": "TAKEN", "CUT": "TAKEN"}.get(row["status"])
        if _act:
            DB.record_decision(sid, _act, qty=data.get("qty"), fill_px=data.get("price"),
                               ts_utc=datetime.now(timezone.utc).isoformat())
        DB.sync_outcomes(SIGNALS)
    return {"ok": True, "status": row["status"]}


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def do_OPTIONS(self): self._send(200, {})

    def do_GET(self):
        if self.path.startswith("/api/signals"):
            refresh_account()
            check_alerts()
            livekeys = ("mid", "pnl_pct", "peak_pct", "iv", "delta", "spread_pct", "alert", "mismatch",
                        "spot", "entry_spot", "spot_dir")
            withlive = lambda p: {**p, "live": {k: MONITOR.get(p["id"], {}).get(k) for k in livekeys}}
            # ONE card per trade: attach each position (open OR closed) to its signal by journal_id
            posmap = {p["journal_id"]: withlive(p) for p in M.load() if p.get("journal_id")}
            _window = SIGNALS[-60:]
            _armed = [s for s in SIGNALS if (s.get("reentry") or {}).get("armed") and s not in _window]
            sigs = []
            for s in reversed(_window + _armed):        # always surface armed re-entry cards, even if older
                s2 = dict(s)
                if s["id"] in posmap:
                    s2["pos"] = posmap[s["id"]]
                if s["id"] in LIVE_SIG:
                    s2["live"] = LIVE_SIG[s["id"]]          # live move% + recommendation for un-taken cards
                sigs.append(s2)
            # CO-SIGN (B3): flag NEW signals whose correlation cluster is called by OTHER providers
            # within the last 30 min — one bet, not N. The US-BETA cluster cap already bounds the risk.
            from collections import defaultdict as _dd
            clus = _dd(set)
            nowt = datetime.now(ET)
            for s in sigs:
                if s.get("status") != "NEW":
                    continue
                try:
                    if (nowt - datetime.fromisoformat(s["iso"])).total_seconds() > 1800:
                        continue
                except Exception:
                    pass
                g = s["sig"]
                clus[D._cluster_key(g.get("ticker"), g.get("type"))].add(s.get("provider"))
            for s in sigs:
                g = s["sig"]
                others = [p for p in clus.get(D._cluster_key(g.get("ticker"), g.get("type")), set())
                          if p and p != s.get("provider")]
                if others and s.get("status") == "NEW":
                    s["cosign"] = sorted(others)
            linked = {s["id"] for s in SIGNALS}
            orphans = [withlive(p) for p in M.open_positions() if p.get("journal_id") not in linked]
            orphans += [withlive(p) for p in M.load() if p.get("kind") == "tracked"]   # provider-tracked shells
            _sc_cal, _sc_exit = _scorecard_cached()
            # ALL-TIME tallies over the FULL store (the feed above is windowed to the last 60 for render
            # speed; the header shows the real totals so the count isn't frozen at 60).
            _tot = {"signals": len(SIGNALS),
                    "taken": sum(1 for s in SIGNALS if s.get("status") in ("TAKEN", "CLOSED")),
                    "skipped": sum(1 for s in SIGNALS if s.get("status") == "SKIPPED")}
            return self._send(200, {"account": E.ACCOUNT, "risk": E.RISK_PCT, "start": START_ACCOUNT,
                                    "now": datetime.now(ET).isoformat(),
                                    "sleeve": SLEEVE, "sources": SOURCES, "discipline": D.state(),
                                    "killed": V.killed_providers(), "day_override": _day_override_active(),
                                    "positions": orphans, "totals": _tot,
                                    "pending": PENDING, "signals": sigs, "stats": ST.performance(),
                                    "calibration": _sc_cal, "exit_quality": _sc_exit,
                                    "provider_edge": _provider_edge_cached(),
                                    "broker": _broker_snap()})
        html = (ROOT / "dashboard.html").read_bytes()
        self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path.startswith("/api/assign"):         # resolve a disambiguation prompt
            pend = next((x for x in PENDING if x["id"] == data.get("pending_id")), None)
            if pend:
                M.assign_to_position(data.get("position_id"), pend["classified"])
                PENDING.remove(pend)
            return self._send(200, {"ok": bool(pend)})
        if self.path.startswith("/api/manage"):        # trader records a real management action
            p = M.apply_action(data.get("id"), data.get("kind"), data.get("qty"),
                               data.get("price"), data.get("stop"))
            refresh_account()                           # closed partials feed the sleeve
            return self._send(200, p or {"error": "unknown position"})
        if self.path.startswith("/api/broker_order"):   # gated order -> tastytrade (Desk Rules enforced)
            return self._send(200, broker_order(data))
        if self.path.startswith("/api/action"):
            return self._send(200, act(data))
        if data.get("watching"):                    # heartbeat from a watching window
            touch_source(data.get("channel") or "?", CHMAP.get(data.get("channel")), display=data.get("name"),
                         healthy=data.get("healthy"), seen=data.get("seen"))
            return self._send(200, {"ok": True, "listening": data.get("channel")})
        return self._send(200, ingest(data) or {"ignored": True})

    def log_message(self, *a): pass


if __name__ == "__main__":
    try:
        M.prune_stale(datetime.now(ET).isoformat())     # drop yesterday's 0DTE tracked/closed shells
    except Exception:
        pass
    if EQUITY_SOURCE == "allocation":
        refresh_account()
        print(f"💹 Compounding: allocation sleeve — start ${START_ACCOUNT:,.0f} → now ${E.ACCOUNT:,.2f}")
    elif EQUITY_SOURCE == "ibkr":
        try:
            import market
            eq = market.equity()
            if eq:
                E.ACCOUNT = eq
                print(f"💹 Compounding: FULL IBKR balance ${eq:,.0f}")
        except Exception:
            print("   (IBKR balance sync failed — using fixed account)")
    else:
        print(f"   Fixed account ${E.ACCOUNT:,.0f} (no compounding)")
    print(f"🟢 Signal Desk → http://localhost:{PORT}   (account ${E.ACCOUNT:,.0f}, {E.RISK_PCT}% risk, "
          f"{len(SEG)} tickers loaded)")
    print("   Open that URL, then turn on 'Watch' in the extension. Ctrl+C to stop.")
    if ENRICH:
        import threading
        threading.Thread(target=monitor_loop, daemon=True).start()
        threading.Thread(target=ustop_loop, daemon=True).start()
        print("📡 Live position monitor started (IBKR option prices + Greeks).")
        print("⚡ Fast underlying-stop poll started (~6s spot watch for provider SL levels).")
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
