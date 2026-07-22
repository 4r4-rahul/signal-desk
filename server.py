#!/usr/bin/env python3
"""
Signal Desk server — receives live Discord signals, runs the engine, and serves a
web dashboard with live cards + Take/Skip (which log to the tracker).

Run:   python3 server.py         (open http://localhost:8787 in a browser)
Env:   ACCOUNT=15000 RISK_PCT=1  python3 server.py
"""
import json, http.server, gzip
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os, re

GZIP_MIN = 1400        # ~one MTU; below this the gzip header costs more than it saves
GZIP_LEVEL = 6         # 6 is the knee: level 9 buys <2% more on this JSON for ~3x the CPU
import deskpaths as _P                       # env-controlled DATA root (prod: identical to code dir)
import signal_engine as E
import tracker as T
import discipline as D
import equity as EQ
import validate as V
import integrations as N
import manage as M
import stats as ST
# --- LLM PARSING LAYER (integration point 1/5) -------------------------------------------
# Optional by construction: if the module or its SDK is missing the desk behaves exactly as it
# did before. Nothing below is allowed to raise into the ingest path.
try:
    import llm_parse as LLM
except Exception:
    LLM = None
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


def _scrub_nan(o):
    """Recursively replace NaN/Infinity floats (from empty IBKR quotes) with None — they are valid in
    Python's json but INVALID JSON that the browser's JSON.parse rejects, blanking the whole dashboard."""
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _scrub_nan(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_scrub_nan(v) for v in o]
    return o


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
            from safe_io import safe_write_json
            safe_write_json(SIGNALS_STORE.parent / "_provider_edge.json", _edge, min_keep_ratio=0.0)  # derived: atomic+backup, no tripwire
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
        return json.loads((_P.CHANNELS / "_channel_map.json").read_text())
    except Exception:
        return {}


CHMAP = load_chmap()
SIGNALS_STORE = _P.CHANNELS / "_signals.json"
INBOUND_LOG = _P.LOGS / "inbound.log"


def load_signals():
    try:
        return json.loads(SIGNALS_STORE.read_text())
    except Exception:
        return []


SIGNALS_CAP = 350   # in-memory ceiling — mirrors the 250 disk window with headroom for kept-alive cards
CHIP_LOG_KEEP = 24  # keep the last N completed chip scalps as the persistent per-provider trade-log
SHADOW_TICK_BATCH = 40   # live shadows ticked per heavy lap, ROTATING so every one is covered (see below)
_SHADOW_RR = [0]         # rotation offset (list so the monitor loop can mutate it)


def trim_signals():
    """Bound in-memory SIGNALS to SIGNALS_CAP so the monitor lap + every poll stay O(CAP), not O(session).
    Keeps the last CAP by insertion order, PLUS any older card still doing work: an armed re-entry watch,
    a live shadow, or a card linked to an OPEN position. Dropping old CLOSED/SKIP dicts is safe — LIVE_SIG
    self-heals (drops absent ids) and MONITOR is keyed by position id, not signal id (no dangling refs)."""
    global SIGNALS
    if len(SIGNALS) <= SIGNALS_CAP:
        return
    try:
        open_jids = {p.get("journal_id") for p in M.open_positions() if p.get("journal_id")}
    except Exception:
        open_jids = set()
    recent = SIGNALS[-SIGNALS_CAP:]
    recent_ids = {id(s) for s in recent}            # identity, not s["id"] (ids can repeat across reposts)
    # keep the last ~24 COMPLETED chip scalps as the persistent trade-log (bounded — no unbounded growth)
    _chip_ended = [s for s in SIGNALS if s.get("provider") == "chip"
                   and (s.get("shadow") or {}).get("state") == "ended"][-CHIP_LOG_KEEP:]
    chip_keep = {id(s) for s in _chip_ended}
    keep_old = [s for s in SIGNALS[:-SIGNALS_CAP] if id(s) not in recent_ids and (
                (s.get("reentry") or {}).get("armed")
                or (s.get("shadow") and s["shadow"].get("state") != "ended")
                or s.get("id") in open_jids
                or s.get("status") in ("NEW", "WATCHING")
                or id(s) in chip_keep)]
    SIGNALS = keep_old + recent                     # preserve chronological order


def save_signals():
    from safe_io import safe_write_json, DeskClobberBlocked
    trim_signals()
    try:
        safe_write_json(SIGNALS_STORE, SIGNALS[-250:], min_keep_ratio=0.5, floor=20)
    except DeskClobberBlocked as e:
        _inlog("-", "SYSTEM", str(e), "CLOBBER")          # LOUD — a blocked write must never look silent
        try: _alert(f"🛑 save_signals blocked a clobber: {e}", "Signal Desk — WRITE BLOCKED")
        except Exception: pass
    except Exception as e:
        _inlog("-", "SYSTEM", f"save_signals error: {e}", "ERROR")


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
        # Pick the NEAREST calendar date matching this MM/DD, not the next FUTURE one. The old rule
        # ("first year where d >= today") turned any slightly-stale signal into a LEAP a year out: on
        # 7/21 a "7/8" callout resolved to 2027-07-08, and the desk then quoted a contract that does not
        # trade (IBKR: "No security definition") on every lap. A bare MM/DD on a scalping desk always
        # means the closest such date. Ties break toward the future so 0DTE-ish signals stay live.
        cands = []
        for yr in (now.year - 1, now.year, now.year + 1):
            try:
                cands.append(datetime(yr, mo, dd, tzinfo=ET))
            except ValueError:
                continue                                   # e.g. 2/29 in a non-leap year
        if not cands:
            return None
        best = min(cands, key=lambda d: (abs((d.date() - now.date()).days),
                                         0 if d.date() >= now.date() else 1))
        return best.strftime("%Y%m%d")
    return None


def _archive_signal(prov, entry):
    """PERMANENT structured archive of every signal — the FULL CARD (sig + score + reasons + the whole
    sizing plan + dynamic stop & its terms + behavior + Greeks). Never pruned. The dataset for future
    analysis / expectancy tracking / honest ML retraining on real live data + real recommendations."""
    try:
        d = _P.CHANNELS / (prov or "unknown")
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
        pj = _P.CHANNELS / prov / "paper_journal.csv"
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
IBKR = {"connected": False, "last_data": None, "mkt_type": None}   # live IBKR market-data link status
_TAKE_PINGS = {}   # {day, n} — daily 🟢 TAKE push-alert counter (anti-spam cap)
LIVE_SIG = {}   # card_id -> live quote + move% + recommendation for an un-taken actionable card
_TAPE_CACHE = {}   # ticker -> (epoch, indicators) — throttled underlying spot+tape for path capture
DAY_OVERRIDE_DATE = None   # ISO date the day-stop is manually overridden (auto-resets each day)
_OVERRIDE_FILE = _P.CHANNELS / "_day_override"   # persisted so a restart doesn't silently re-block


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
    # fillable ticket -> stated premium -> live quote mid. The last fallback matters: a callout with no
    # stated price is still fully shadowable off the real market, and refusing lost the whole trade story.
    ref = tk.get("limit") or sg.get("premium") or (entry.get("opt") or {}).get("mid")
    if not ref:
        return
    entry["shadow"] = {"ref": ref, "stop_pct": (entry.get("plan") or {}).get("stop_pct") or tk.get("stop_pct") or 30,
                       "t1_pct": tk.get("t1_pct") or 30,
                       "expiry": _resolve_expiry(sg) or (entry.get("opt") or {}).get("expiry"),
                       "start_utc": datetime.now(timezone.utc).isoformat(), "state": "live",
                       "result": None, "peak": 0.0, "trough": 0.0, "now": 0.0}


_CHIP_PHASE_LABELS = {"ENTERED": "taking", "MANAGING": "managing", "T1_HIT": "½ booked",
                      "T2_HIT": "¾ booked", "RUNNER": "runner", "TRAILED_OUT": "trailed out",
                      "STOPPED": "cut", "RESOLVED": "closed"}


def _chip_narration(phase, sh, t1, t2, t3, stop, blended_pct, blended_r):
    """Human-readable lifecycle log — reconstructable from current fields alone (peak is monotonic, so every
    crossed milestone is known). Numbers come straight from ticket/shadow; the closed line's R is the SAME
    scale-out proxy as blended_r, so narration can never contradict the scored result."""
    peak, now = sh.get("peak", 0.0), sh.get("now")
    lines = [f'📈 taking @ {sh.get("ref")}']
    if peak >= t1:
        lines.append(f'½ booked at +{t1:.0f}% → stop to breakeven')
    if peak >= t2:
        lines.append(f'¼ booked at +{t2:.0f}% → trailing +{t1:.0f}%')
    if peak >= t3:
        lines.append(f'runner target +{t3:.0f}% tagged (peak +{peak:.0f}%)')
    if phase == "STOPPED":
        lines.append(f'cut −{stop:.0f}% at stop (−1R)')
    elif phase == "TRAILED_OUT" and now is not None:
        lines.append(f'runner exited +{now:.0f}%')
    elif phase == "RESOLVED" and blended_pct is not None:
        icon = "✅" if (blended_r or 0) > 0 else "❌"
        lines.append(f'{icon} closed {blended_pct:+.0f}% ({blended_r:+.1f}R)')
    return lines


_CHIP_TAPE_BAR, _CHIP_TAPE_FLOOR = 62, 52      # chip's entry bar / the desk's no-edge floor
_CHIP_VWAP_HOLD, _CHIP_VWAP_LOST = 0.03, -0.05  # % of price, in the trade's favour
_CHIP_DRIFT_BAND = -12                          # a full strong→moderate grade-band slide


def _fin2(x, nd=2):
    try:
        x = float(x)
        return round(x, nd) if x == x and abs(x) != float("inf") else None
    except Exception:
        return None


def _chip_px(entry, now_pct, t1, t2, t3, cur_stop, stop, sh):
    """Live $ ladder derived from entry×(1+pct/100). NEVER read ticket.t1/$ — those can be the generic
    ladder. `mark` is the option MID (what shadow.now is computed off); `out` is the bid (real exit)."""
    if not entry:
        return None
    tick = 0.05 if entry >= 3 else 0.01
    rd = lambda x: round(round(x / tick) * tick, 2)
    at = lambda p: rd(entry * (1 + (p or 0) / 100.0))
    mark, spread, bid = at(now_pct), sh.get("spread_pct"), sh.get("bid")
    out = _fin2(bid) if bid else (rd(mark * (1 - spread / 200.0)) if spread else None)
    t1p, stopp = at(t1), at(cur_stop)
    return {"entry": _fin2(entry), "mark": mark, "out": out, "spread_pct": _fin2(spread, 1),
            "t1": t1p, "t2": at(t2), "t3": at(t3), "stop": stopp,
            "to_t1_pct": _fin2((t1 or 0) - (now_pct or 0), 1), "to_t1_cx": int(round((t1p - mark) * 100)),
            "to_stop_pct": _fin2((now_pct or 0) - (cur_stop or 0), 1),
            "to_stop_cx": int(round((mark - stopp) * 100)),
            "open_r": _fin2((now_pct or 0) / abs(stop), 2) if stop else None, "mark_source": "mid"}


def _chip_clock(start_utc, ended_utc, er, sh, phase):
    """Held-for + session clock + HONEST 0DTE theta burn (extrinsic ≈ k·√T, so decay is back-loaded —
    a flat theta/6.5 understates the last hour badly)."""
    import math
    try:
        t0 = datetime.fromisoformat(start_utc) if start_utc else None
    except Exception:
        t0 = None
    if not t0:
        return None
    end = None
    if ended_utc:
        try: end = datetime.fromisoformat(ended_utc)
        except Exception: end = None
    held = int(((end or datetime.now(timezone.utc)) - t0).total_seconds())
    etn = datetime.now(ET)
    close = etn.replace(hour=16, minute=0, second=0, microsecond=0)
    dte = (er.get("dte") or {}).get("dte")
    # a 1DTE/2DTE contract has FULL sessions left beyond today's close — without the 390·dte term the
    # theta narrative on a multi-day card was wrong by an entire session.
    mins_close = max(0, int((close - etn).total_seconds() / 60)) + 390 * int(dte or 0)
    h = mins_close / 60.0
    tp = None
    if sh.get("theta") and sh.get("mark"):
        tp = _fin2(abs(sh["theta"]) / sh["mark"] * 100, 0)          # LIVE theta
    src = "live" if tp is not None else "entry"
    if tp is None:
        tp = (er.get("dte") or {}).get("theta_pct")
    burn_next = None
    if dte == 0 and h > 0:
        burn_next = int(round((1 - math.sqrt(max(h - 1, 0) / h)) * 100))
    band, warn = "normal", None
    if dte == 0:
        hhmm = etn.hour * 100 + etn.minute
        if hhmm >= 1545:
            band, warn = "eod", "⏰ EOD — flat the 0DTE before the close"
        elif mins_close <= 90:
            band, warn = "late", "⏳ late-session 0DTE — θ dominates from here; it needs the move NOW"
        elif etn.hour >= 13:
            band, warn = "cliff", "⏱ past 13:00 — chip wouldn't OPEN a 0DTE here"
    hl = f"{held // 3600}h {held % 3600 // 60:02d}m" if held >= 3600 else f"{held // 60}m"
    cl = f"{mins_close // 60}h {mins_close % 60:02d}m to close" if mins_close >= 60 else f"{mins_close}m to close"
    line = f"held {hl} · {cl}"
    if tp is not None:
        line += (f" · θ ≈ −{burn_next}%/hr from here (0DTE, accelerating)" if burn_next is not None
                 else f" · θ ≈ −{tp}%/day")
    return {"held_s": held, "held_label": hl, "mins_to_close": mins_close, "close_label": cl,
            "dte": dte, "theta_pct_day": tp, "theta_source": src, "burn_next_hr": burn_next,
            "band": band, "line": line, "warn": (None if phase in ("STOPPED", "TRAILED_OUT", "RESOLVED") else warn)}


def _chip_giveback(peak, now, entry, result, t1):
    """How much of the peak has been surrendered. Thresholds are the desk's OWN exit_rule numbers."""
    if not peak or peak < 5:
        return None
    pts = _fin2(peak - (now or 0), 1)
    if pts is None or pts < 5:
        return None
    usd = _fin2((entry or 0) * (peak - (now or 0)) / 100.0) if entry else None
    lvl = "watch"
    if pts >= 25 and peak >= 30:
        lvl = "exit"
    elif pts >= 17:
        lvl = "rule"
    line = f"gave back {pts:.0f} pts — peaked +{peak:.0f}%, now {now:+.0f}%"
    if lvl == "rule":
        line += " — the book's winners top out at ~17"
    elif lvl == "exit":
        line += " — that's the desk's trail-exit (25 off peak)"
    if result == "LOSS" and t1 and peak >= t1:
        line = f"round-tripped +{peak:.0f}% → {now:+.0f}%"
    return {"pts": pts, "usd": usd, "cx": int(round((usd or 0) * 100)), "peak": _fin2(peak, 1),
            "now": _fin2(now, 1), "level": lvl, "line": line, "round_trip": False}


def _chip_thesis(er, lr, reason, side, phase, t1, cur_stop, stop):
    """Is chip's ENTRY REASON still true? A level-thesis (VWAP reclaim) dies on the level alone; a
    momentum-thesis needs both the level AND the tape gone. Ordered — first match wins."""
    if not er:
        return None
    tae = er.get("tape_at_entry") or {}
    s_in = tae.get("score")
    kind = "level" if ("VWAP" in (reason or "") or (er.get("trigger") or {}).get("trigger_type", "").startswith("vwap")) else "momentum"
    done = phase in ("STOPPED", "TRAILED_OUT", "RESOLVED")
    if not lr or lr.get("stale") or lr.get("score") is None or lr.get("vfav") is None:
        return {"state": "unknown", "kind": kind, "icon": "·", "word": "NO READ", "why": "no live tape read",
                "line": "· no live read — chip's tape is stale", "action": "", "score_at_entry": s_in}
    s, g, vf = lr["score"], lr.get("grade"), lr["vfav"]
    drift = (s - s_in) if (s_in is not None) else 0
    rsi, ema = lr.get("rsi"), lr.get("ema_side")
    healthy = (45 <= (rsi or 50) <= 72) if side == "C" else (28 <= (rsi or 50) <= 55)
    if g == "against":                                   st, why = "broken", "tape flipped against"
    elif s < _CHIP_TAPE_FLOOR:                           st, why = "broken", f"tape collapsed to {s}"
    elif kind == "level" and vf <= _CHIP_VWAP_LOST:      st, why = "broken", f"lost VWAP ({vf:+.2f}%)"
    elif kind == "momentum" and vf <= _CHIP_VWAP_LOST and s < _CHIP_TAPE_BAR:
        st, why = "broken", f"lost VWAP and tape {s} < {_CHIP_TAPE_BAR}"
    elif s < _CHIP_TAPE_BAR:                             st, why = "weakening", f"tape {s} below chip's {_CHIP_TAPE_BAR} bar"
    elif vf <= _CHIP_VWAP_HOLD:                          st, why = "weakening", f"VWAP contested ({vf:+.2f}%)"
    elif drift <= _CHIP_DRIFT_BAND:                      st, why = "weakening", f"conviction faded {s_in}→{s}"
    elif ema == "against" and not healthy:               st, why = "weakening", f"EMA turned, RSI {rsi:.0f} rolling"
    else:                                                st, why = "intact", f"still {'above' if vf > 0 else 'at'} VWAP ({vf:+.2f}%)"
    if s_in is not None:
        why += f", tape {s} vs {s_in} in"
    ic = {"intact": "✅", "weakening": "⚠️", "broken": "🛑"}[st]
    wd = {"intact": "INTACT", "weakening": "WEAKENING", "broken": "BROKEN"}[st]
    act = ""
    if not done:
        hit1 = phase not in ("ENTERED", "MANAGING")
        if st == "broken":
            act = (f"runner's thesis is gone — take the trail at +{cur_stop:.0f}%" if hit1
                   else f"structure's gone — don't wait for the −{stop:.0f}% stop; cut or tighten now")
        elif st == "weakening":
            act = f"tighten — thesis is soft, don't add" if hit1 else f"almost there with a fading tape — bank the base at +{t1:.0f}%"
        else:
            act = f"runner's earned it — trail at +{cur_stop:.0f}%" if hit1 else f"let it work toward T1 +{t1:.0f}%"
    return {"state": st, "kind": kind, "icon": ic, "word": wd, "why": why, "line": f"{ic} thesis {st} — {why}",
            "action": act, "vfav": _fin2(vf, 2), "vwap": _fin2(lr.get("vwap")), "spot": _fin2(lr.get("spot")),
            "score": s, "score_at_entry": s_in, "drift": drift, "ema_side": ema, "rsi": _fin2(rsi, 0),
            "stale": False, "age_s": lr.get("age_s")}


def _chip_drift(er, lr):
    """Which tape FACTOR moved since entry (VWAP/EMA/RSI/room) — same producer both sides, exact join."""
    if not er or not lr:
        return None
    ein = {f.get("factor"): f.get("score") for f in ((er.get("tape_at_entry") or {}).get("factors") or [])}
    now = {f.get("factor"): f.get("score") for f in (lr.get("factors") or [])}
    if not ein or not now:
        return None
    W = {"VWAP": 0.35, "EMA": 0.25, "RSI": 0.25, "room": 0.15}
    rows, flipped = [], []
    for k in ("VWAP", "EMA", "RSI", "room"):
        a, b = ein.get(k), now.get(k)
        if a is None or b is None:
            continue
        d = round(b - a, 2)
        fl = (a > 0.05 and b < -0.05) or (a < -0.05 and b > 0.05)
        if fl:
            flipped.append(k)
        if fl or abs(d) >= 0.20:
            rows.append({"factor": k, "in": round(a, 2), "now": round(b, 2), "d": d,
                         "cd": round(d * W.get(k, 0), 3), "flipped": fl})
    si, sn = (er.get("tape_at_entry") or {}).get("score"), lr.get("score")
    if si is None or sn is None:
        return None
    line = f"conviction {si} → {sn} ({sn - si:+d})" + (f" · {'+'.join(flipped)} flipped" if flipped else "")
    return {"score_in": si, "score_now": sn, "delta": sn - si, "flipped": flipped, "rows": rows, "line": line}


def _chip_live_read(s):
    """O(1) join to chip's live tape by ticker+side, id-gated so a NEW scalp can't inherit an old read."""
    import time
    try:
        import chip_desk as CD
    except Exception:
        return None
    sg = s.get("sig") or {}
    lr = (CD.STATUS.get(f'{sg.get("ticker")}|{sg.get("type")}') or {}).get("live_read")
    if not lr or lr.get("sid") != s.get("id"):
        return None
    age = time.time() - (lr.get("ts") or 0)
    return {**lr, "age_s": round(age), "stale": age > 90}


def _chip_lifecycle(s):
    """Derive the live scalp lifecycle for a chip card from its shadow + ticket (pure, O(1), no I/O).
    Phase progression is peak-based — exactly how db._shadow_scaleout books ½@t1 / ¼@t2 — so the card's
    phase, current stop, and narrated R stay consistent-by-construction with the scored discipline."""
    import db as DB
    sh, tk, sg = s.get("shadow") or {}, s.get("ticket") or {}, s.get("sig") or {}
    meta = s.get("chip") or {}
    t1, t2, t3, stop = tk.get("t1_pct"), tk.get("t2_pct"), tk.get("t3_pct"), tk.get("stop_pct")
    if None in (t1, t2, t3, stop):                       # a chip card must carry all four targets
        return None
    now, peak, trough = sh.get("now"), sh.get("peak", 0.0), sh.get("trough", 0.0)
    state, result = sh.get("state"), sh.get("result")
    hit1, hit2, hit3 = peak >= t1, peak >= t2, peak >= t3
    cur_stop = -stop if not hit1 else (0.0 if not hit2 else t1)
    stopped = (not hit1) and (result == "LOSS" or (now is not None and now <= -stop))
    trailed = hit1 and (now is not None and now <= cur_stop)
    if state == "ended":               phase = "RESOLVED"
    elif stopped:                      phase = "STOPPED"
    elif trailed:                      phase = "TRAILED_OUT"
    elif hit3:                         phase = "RUNNER"
    elif hit2:                         phase = "T2_HIT"
    elif hit1:                         phase = "T1_HIT"
    elif peak == 0 and (now or 0) == 0: phase = "ENTERED"
    else:                              phase = "MANAGING"
    # blended-R proxy: run the SAME scale-out discipline over a representative 2-point path
    _er = meta.get("entry_read") or {}
    _lr = _chip_live_read(s) if sh.get("state") == "live" else None
    ticks = [-stop] if stopped else [peak, now if now is not None else peak]
    blended_pct = DB._shadow_scaleout(ticks, t1, t2, stop, t3)
    blended_r = round(blended_pct / abs(stop), 2) if (blended_pct is not None and stop) else None
    return {"phase": phase, "phase_label": _CHIP_PHASE_LABELS.get(phase, phase),
            "entry_px": sh.get("ref"), "leg": f'{sg.get("ticker")} {sg.get("strike")}{sg.get("type")}',
            "cp": sg.get("type"), "now_pct": now, "peak_pct": peak, "trough_pct": trough,
            "cur_stop_pct": cur_stop, "t1_pct": t1, "t2_pct": t2, "t3_pct": t3, "stop_pct": stop,
            "hit": [hit1, hit2, hit3], "stopped": bool(stopped), "result": result,
            "blended_pct": blended_pct, "blended_r": blended_r,
            "opened": sh.get("start_utc"), "closed": sh.get("ended_utc"),
            "reason": meta.get("reason"), "regime": meta.get("regime"), "news": meta.get("news"),
            "entry_read": _er,                          # frozen WHY (strike/DTE/tape at fill) for the story
            "px": _chip_px(sh.get("ref"), now, t1, t2, t3, cur_stop, stop, sh),
            "clock": _chip_clock(sh.get("start_utc"), sh.get("ended_utc"), _er, sh, phase),
            "give_back": _chip_giveback(peak, now, sh.get("ref"), result, t1),
            "thesis": _chip_thesis(_er, _lr, meta.get("reason"), sg.get("type"), phase, t1, cur_stop, stop),
            "drift": _chip_drift(_er, _lr),
            "narration": _chip_narration(phase, sh, t1, t2, t3, stop, blended_pct, blended_r)}


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


def entry_timing(sig, ind, reco, u_stop=None, spot=None, provider=None):
    """ONE clear 'when to pull the trigger' verdict — TAKE / WAIT / SKIP with a watchable trigger.
    PROVIDER-AWARE and shadow-backtested: a trusted provider clears TAKE on a lower tape bar (the old
    fixed grade>=moderate bar fired ~never — every real tape landed 'weak'). Also: AGAINST->SKIP,
    collapsed<=-30->SKIP, faded -30..-15->WAIT, TAKE requires chase>=-15, wider chase cap when confirmed."""
    import market as _MK
    call = (sig.get("type") or "C").upper() != "P"
    tk, prem = sig.get("ticker"), sig.get("premium")
    st = _MK.tape_strength(sig.get("type"), ind) if ind else {"grade": "no-data", "score": None}
    grade, score = st.get("grade"), st.get("score")
    spot = spot if spot is not None else (ind or {}).get("price")
    vwap = (ind or {}).get("vwap")
    vw = f"{vwap:.2f}" if vwap else "VWAP"
    chase = (reco or {}).get("chase_pct", 0)
    reclaim = f"reclaims VWAP {vw}" if call else f"loses VWAP {vw}"
    holds = f"holds above VWAP {vw} & RSI lifts" if call else f"holds below VWAP {vw} & RSI drops"
    try: emult = E.provider_edge_mult(provider) if provider else 1.0
    except Exception: emult = 1.0
    trusted = emult >= 1.0
    min_tape = 52 if trusted else (74 if emult < 0.5 else 60)      # provider-tiered TAKE bar (shadow-validated)
    def out(a, i, h, t=None): return {"action": a, "icon": i, "head": h, "trig": t}
    # ---- hard SKIPs (shadow-validated junk) ----
    if u_stop and spot and ((spot <= u_stop) if call else (spot >= u_stop)):
        return out("SKIP", "🔴", f"{tk} {spot:.2f} is past the {u_stop} stop — thesis broken")
    if chase <= -30:                                              # collapsed (was -40; -36% HIMS slipped through)
        return out("SKIP", "🔴", f"down {chase}% since the call — the setup likely failed")
    if grade == "against":                                        # shadow: AGAINST = 11% win / -0.62R -> kill it
        return out("SKIP", "🔴", "tape is AGAINST this direction — do not take", f"only if {tk} {reclaim} first")
    # ---- chase cap: wider when the tape is CONFIRMED, +bonus for trusted; ROLL above it ----
    _cap = (60 if grade == "strong" else 40 if grade == "moderate" else 25) + (15 if trusted else 0)
    if chase >= _cap:
        _trig = f"take on a pullback to ~${round((prem or 0) * 1.08, 2)}"
        if grade in ("strong", "moderate"):
            _trig += " — or 🎯 roll to a fresh ~0.45Δ strike (tape confirmed, the called strike ran)"
        return out("WAIT", "🟡", f"chasing +{chase}% above the call — paying up", _trig)
    if u_stop and spot and abs(spot - u_stop) / spot < 0.0015:    # no room to the stop
        return out("WAIT", "🟡", f"right on top of the {u_stop} stop — no room", f"take once {tk} pushes ~0.3 off {u_stop}")
    if -30 < chase < -15:                                         # faded but not collapsed -> wait for the turn
        return out("WAIT", "🟡", f"faded {chase}% since the call — wait for the tape to turn", f"take when {tk} {holds}")
    # ---- no-data tape: don't auto-WAIT (shadow: +0.26R). trusted + in-zone -> surface as TAKE w/ caveat ----
    if grade == "no-data" or score is None:
        if trusted and chase < _cap:
            return out("TAKE", "🟢", f"{provider or 'signal'} in zone ({chase:+d}%) — trusted; tape read pending", "take at the limit; eyeball that the tape hasn't turned")
        return out("WAIT", "⚪", "tape read pending — hold for confirmation", f"take when {tk} {holds}")
    # ---- THE TAKE gate: score clears the provider-tiered bar, in zone, room ----
    if score >= min_tape:
        # Say WHY this is a take. The grade label and the TAKE bar are different scales -- "weak" is
        # anything under 62, but the bar is 52 for a trusted provider -- so a 56 legitimately clears it
        # while still reading "weak". Printing "tape weak (56) ... TAKE" without the bar looked like a
        # bug and cost trust in the card. Show the bar and who it belongs to.
        _bar = (f"clears the {min_tape} bar for {provider}" if provider else f"clears the {min_tape} bar")
        _cav = " — thin tape, size accordingly" if score < 62 else ""
        return out("TAKE", "🟢",
                   f"tape {score} {_bar}{' (trusted)' if trusted else ''}, in buy zone ({chase:+d}%), "
                   f"room to the stop{_cav}", "take at the limit now")
    return out("WAIT", "⚪", f"tape {grade} ({score}) — below the {min_tape} bar for {provider or 'this provider'}",
               (f"take when {tk} {holds}" if vwap else "take when the tape strengthens"))


def monitor_loop():
    import market as MK
    import time
    conn = None
    cyc = 0
    while True:
        try:
            if conn is None:
                conn = MK.connect(client_id=80)
                try: conn.reqMarketDataType(int(MK.env("IBKR_MARKET_DATA_TYPE", "2")))
                except Exception: pass
            IBKR["connected"] = bool(conn and conn.isConnected())      # live market-data link status
            IBKR["mkt_type"] = MK.env("IBKR_MARKET_DATA_TYPE", "2")
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
                    IBKR["last_data"] = datetime.now(ET).isoformat()      # live quote landed = data flowing
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
                except Exception:
                    continue
            # live-quote the ACTIONABLE un-taken cards -> live move% + recommendation on the card
            # PRUNE the decision surface: expired contracts + collapsed/worthless corpses (from last lap's
            # live read) must not crowd fresh signals out of the [-20:] cap. ~58% of cards were dead corpses.
            _today = datetime.now(ET).strftime("%Y%m%d")

            def _dead(s):
                lv = LIVE_SIG.get(s["id"]) or {}
                if (lv.get("chase_pct") is not None and lv["chase_pct"] <= -30) or (lv.get("mid") is not None and lv["mid"] <= 0.05):
                    return True                              # collapsed / worthless since the call
                e = _resolve_expiry(s.get("sig") or {}) or (s.get("opt") or {}).get("expiry")
                return bool(e) and len(str(e)) == 8 and str(e) < _today   # expired contract

            act = [s for s in SIGNALS if s.get("status") in ("NEW", "WATCHING") and s.get("verified")
                   and (not s.get("paper") or s.get("manual_override")) and (s.get("sig") or {}).get("premium")
                   and not _dead(s)]
            for s in act[-20:]:                              # cap: 20 most-recent LIVE actionable cards
                try:                                          # one bad/ambiguous contract must NOT kill the cycle
                    sg = s["sig"]
                    exp = _resolve_expiry(sg) or (s.get("opt") or {}).get("expiry")   # SIGNAL'S expiry wins
                    q = MK.option_quote(sg["ticker"], str(sg["strike"]).split(".")[0], sg["type"],
                                        expiry=exp, ib=conn)
                    if not q or q.get("error") or q.get("mid") is None:
                        continue
                    IBKR["last_data"] = datetime.now(ET).isoformat()      # live quote landed = data flowing
                    if (s.get("opt") or {}).get("expiry") != q.get("expiry"):   # heal a wrong-expiry snapshot
                        s["opt"] = q
                        _pl = s.get("plan") or {}
                        _e = _pl.get("entry") or sg.get("premium") or 0
                        _t2f = (_pl["tp2"] / _e - 1) if (_e and _pl.get("tp2")) else 0.70
                        s["ticket"] = E.order_ticket(q, sg.get("premium"), _pl.get("stop_pct") or 30,
                                                     (_pl.get("tp1_pct") or 35) / 100.0, _t2f)
                        if s.get("provider") == "chip":       # restore chip's own ladder over the rebuild
                            _chip_ladder(s["ticket"], s.get("chip_plan"))
                        save_signals()
                    r = _live_reco(sg.get("premium"), q["mid"], q.get("ask"), q.get("spread_pct"))
                    if r:
                        _ind = _tape_cached(conn, sg["ticker"])                 # fresh tape for the timing verdict
                        _ss = sg.get("stated_stop") or {}
                        _us = _ss.get("value") if _ss.get("kind") == "underlying" else None
                        r["entry"] = entry_timing(sg, _ind, r, u_stop=_us, spot=(_ind or {}).get("price"),
                                                  provider=s.get("provider"))
                        r["strength"] = MK.tape_strength(sg.get("type"), _ind)   # FRESH tape for the card meter (not the frozen ingest snapshot)
                        # Ping on the FLIP to TAKE, capped so it's actionable-not-spam (max/day + one per
                        # contract until it leaves TAKE). This is the alert that was silent on a +418% day.
                        prev_act = (LIVE_SIG.get(s["id"], {}).get("entry") or {}).get("action")
                        if r["entry"]["action"] == "TAKE" and prev_act != "TAKE":
                            _d = datetime.now(ET).strftime("%Y%m%d")
                            if _TAKE_PINGS.get("day") != _d:
                                _TAKE_PINGS.clear(); _TAKE_PINGS["day"] = _d; _TAKE_PINGS["n"] = 0
                            if _TAKE_PINGS.get("n", 0) < 8:      # anti-spam: <= 8 TAKE pings/day
                                _TAKE_PINGS["n"] = _TAKE_PINGS.get("n", 0) + 1
                                tk = s.get("ticket") or {}
                                leg = f'{sg["ticker"]} {sg["strike"]}{sg["type"]}'
                                _alert(f"🟢 TAKE NOW — {s['provider']}: {leg} — {r['entry']['head']} · "
                                       f"enter ~${q.get('ask') or r['mid']}, stop ${tk.get('stop', '?')} · "
                                       f"T1 ${tk.get('t1','?')} / T2 ${tk.get('t2','?')}", "Signal Desk — 🟢 TAKE")
                        LIVE_SIG[s["id"]] = r
                except Exception:
                    continue
            for k in list(LIVE_SIG):                          # drop stale ids no longer in the feed
                if k not in {s["id"] for s in SIGNALS}:
                    LIVE_SIG.pop(k, None)
            cyc += 1
            _heavy = (cyc % 5 == 0)                            # shadow tracking + DB sync every ~5th lap (~20s);
            # prices/greeks re-read every lap (~4s) off persistent streams. SHADOW TRACK skipped signals over
            # their FULL LIFE: disciplined would-be result (first T1/stop) + ultimate peak/trough, until expiry.
            shadows = ([s for s in SIGNALS if s.get("shadow") and s.get("status") not in ("TAKEN", "CLOSED")
                        and s["shadow"].get("state") != "ended" and (s.get("sig") or {}).get("premium")]
                       if _heavy else [])
            today = datetime.now(ET).strftime("%Y%m%d")
            now_utc = datetime.now(timezone.utc).isoformat()
            dirty = False
            # SWEEP: retire any actionable card whose contract already expired. The per-shadow branch below
            # only fires for shadows still being ticked; a shadow that ENDED earlier never re-enters it, so
            # its card stayed "NEW" forever. That is how 4 dead 07/21 contracts were still being offered as
            # live trades at the next morning's open. Cheap (bounded by SIGNALS_CAP) and status-only.
            if _heavy:
                for _s in SIGNALS:
                    if _s.get("status") not in ("NEW", "WATCHING"):
                        continue
                    _e = ((_s.get("shadow") or {}).get("expiry") or (_s.get("opt") or {}).get("expiry")
                          or _resolve_expiry(_s.get("sig") or {}))
                    if _e and str(_e) < today:
                        _s["status"] = "EXPIRED"
                        dirty = True
            # ROUND-ROBIN, not "newest 20". The old `shadows[-20:]` silently starved every shadow beyond
            # the 20 most recent: on a busy multi-provider day the older ones stopped receiving path ticks
            # entirely, so their story just stopped being recorded mid-trade. Rotating covers ALL live
            # shadows while keeping per-lap work (and the IBKR line budget) bounded. At 40/lap and ~30s
            # between heavy laps, a shadow is re-touched well inside reap_subs' 90s idle window, so its
            # stream stays warm rather than churning.
            if shadows:
                _n = len(shadows)
                _k = min(_n, SHADOW_TICK_BATCH)
                _off = _SHADOW_RR[0] % _n
                _batch = (shadows + shadows)[_off:_off + _k]
                _SHADOW_RR[0] = (_off + _k) % _n
            else:
                _batch = []
            for s in _batch:
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
                        # ...and RETIRE the card. Ending the shadow was not enough: status stayed "NEW", so
                        # every morning the board offered yesterday's dead 0DTEs as live trades. Found 4 of
                        # them sitting actionable at today's open, all on 07/21 contracts. Status only, so
                        # the card and its whole path are preserved -- nothing is deleted.
                        if s.get("status") in ("NEW", "WATCHING"):
                            s["status"] = "EXPIRED"
                        dirty = True
                        continue
                    q = MK.option_quote(sg["ticker"], str(sg["strike"]).split(".")[0], sg["type"], expiry=exp, ib=conn)
                    if not q or q.get("error") or q.get("mid") is None:
                        continue
                    pct = round((q["mid"] / sh["ref"] - 1) * 100, 1)
                    sh["now"] = pct
                    sh["peak"] = round(max(sh.get("peak", 0), pct), 1)
                    sh["trough"] = round(min(sh.get("trough", 0), pct), 1)
                    # stash the live quote we already have — powers live θ + the real exit (bid) on the card
                    sh["mark"], sh["bid"], sh["ask"] = q.get("mid"), q.get("bid"), q.get("ask")
                    sh["spread_pct"], sh["theta"], sh["delta"] = q.get("spread_pct"), q.get("theta"), q.get("delta")
                    sh["gamma"], sh["iv"] = q.get("gamma"), q.get("iv")   # gamma moves P(T1) ~16 pts; iv is σ
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
                except Exception:
                    continue
            if dirty:
                save_signals()
            if DB and _heavy:
                DB.sync_outcomes(SIGNALS)             # capture shadow resolutions + realized closes
            try: MK.reap_subs(conn)                   # cancel idle streaming lines (IBKR line budget)
            except Exception: pass
            conn.sleep(4)                             # fast lap: persistent streams refresh in-place
        except Exception:
            IBKR["connected"] = False                     # link dropped -> rail shows IBKR down
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
            try: MK.reap_subs(conn)                           # line budget (shares the pool with the monitor)
            except Exception: pass
            time.sleep(2)                                     # spot reads are instant now -> tighter stop poll
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
    # The provider often states the EXPIRY (and stop) in the FILL, not the watch ("expiry july 20 im in
    # at 2.95 stop limit 2.20"). Grab them so the card prices the REAL contract, not the nearest-expiry
    # default — this was the QQQ 700C misread (a 7/20 swing priced as 0DTE → false 'setup failed').
    try:
        _dte, _exp = E._parse_expiry(text)
        if _exp and _exp.upper() != "0DTE":
            w["sig"]["expiry"] = _exp
            if _dte is not None: w["sig"]["dte"] = _dte
            w.pop("opt", None)                           # force a re-quote on the corrected expiry
            w["fill_expiry"] = _exp
    except Exception:
        pass
    try:                                                 # stated premium stop in the fill ("stop limit 2.20")
        _slm = re.search(r'\bs\s*/?\s*l\s*:?\s*(?:at|to|limit)?\s*\$?(\d+(?:\.\d{1,2})?)', text.lower())
        if _slm:
            _sv = float(_slm.group(1))
            w["sig"]["stated_stop"] = {"value": _sv, "kind": "pct" if "%" in text else ("underlying" if _sv >= 50 else "premium")}
    except Exception:
        pass
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


def _chip_ladder(tk, cp):
    """Overlay chip's per-ticker R-ladder onto an order ticket and RE-DERIVE the $ levels off the limit.
    Must be called at ingest AND after ANY ticket rebuild (the wrong-expiry heal at ~1084 constructs a
    fresh ticket from plan.tp1_pct, which chip never sets — silently reverting 22/46/74 to 35/70/110)."""
    if tk is None or not cp:      # NB: `not tk` would skip a brand-new EMPTY ticket dict
        return tk
    tk.update(t1_pct=cp["t1_pct"], t2_pct=cp["t2_pct"], t3_pct=cp["t3_pct"], stop_pct=cp["stop_pct"])
    e = tk.get("limit")
    if e:
        tick = 0.05 if e >= 3 else 0.01
        rd = lambda x: round(round(x / tick) * tick, 2)
        tk["t1"], tk["t2"], tk["t3"] = (rd(e * (1 + cp["t1_pct"] / 100.0)),
                                        rd(e * (1 + cp["t2_pct"] / 100.0)),
                                        rd(e * (1 + cp["t3_pct"] / 100.0)))
        tk["stop"] = rd(e * (1 - cp["stop_pct"] / 100.0))
        risk = e - tk["stop"]
        if risk > 0:
            tk["rr1"], tk["rr2"], tk["rr3"] = (round((tk["t1"] - e) / risk, 2),
                                               round((tk["t2"] - e) / risk, 2),
                                               round((tk["t3"] - e) / risk, 2))
    return tk


# ============ LLM PARSING LAYER (integration point 2/5) ==================================
# The LLM's ONLY job is to rewrite an ambiguous message into an unambiguous one. It then goes
# back through ingest() from the top, so dedup, contract verification, the price-mismatch
# guard, the junk gate, sizing, shadowing and the DB all run EXACTLY as they do for a regex
# parse. Not one guard is bypassed, and none of them has to trust a model output — which is
# what "guards must not depend on an unverifiable model output" means in code.

def _llm_canonical(sig):
    """A parse rendered as text the DETERMINISTIC parser can read on its own."""
    if not sig.get("ticker") or not sig.get("strike") or not sig.get("type"):
        return None
    out = f'{sig["ticker"]} {sig["strike"]}{sig["type"]}'
    exp = sig.get("expiry")
    if exp:
        # The model returns full ISO dates ("2026-07-31"). _parse_expiry does extract 07/31 from
        # that, but only via regex backtracking — and it has also been seen returning the wrong
        # YEAR. Normalise to the unambiguous MM/DD the desk's expiry parser is built for, so the
        # contract we price never depends on either of those.
        m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', str(exp).strip())
        exp = f"{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else exp
        out += f' {exp}'
    if sig.get("premium"):
        out += f' @{sig["premium"]}'
    return out


def _llm_review(text, prov, res, entry, c, data):
    """Queue a hard case. Returns immediately — the network call happens on a worker thread, so
    the single-threaded server keeps serving. The card the desk shows RIGHT NOW is unchanged."""
    if not LLM or not prov or data.get("_from_llm"):
        return
    try:
        sig = (res or {}).get("sig")
        why = LLM.triggers(text, regex_sig=sig, carded=bool(entry),
                           market_mid=(entry or {}).get("opt", {}).get("mid") if entry else None,
                           is_watch=E.is_watch(text),
                           maybe_entry=(not entry) and _maybe_missed_entry(text, c))
        if not why:
            return
        LLM.submit(text, provider=prov, why=why,
                   card_id=(entry or {}).get("id"), extra={"regex_sig": sig})
        _inlog(data.get("channel"), prov, text, "LLM?")
    except Exception:
        pass                                          # review is best-effort, never load-bearing


def _apply_llm_result(r):
    """Runs on the MAIN thread (called from drain points). Applies one finished review."""
    d = r.get("decision") or {}
    act, prov = d.get("action"), (r.get("job") or r).get("provider")
    text = r.get("text") or ""
    if act in (None, "DROP"):
        return
    if act == "ANNOTATE":
        # The regex card stands. We attach the disagreement so the human sees BOTH readings
        # instead of the desk silently preferring one of them.
        card = next((s for s in SIGNALS if s.get("id") == r.get("card_id")), None)
        if card:
            card["llm_review"] = {"disagreements": d.get("disagreements"), "reason": d.get("reason"),
                                  "llm": d.get("sig"), "confidence": d.get("confidence")}
            save_signals()
            _alert(f"⚠ {prov}: Chip reads this differently — {d.get('reason')}\n\"{text[:140]}\"",
                   "Signal Desk — Parse disagreement")
        return
    canonical = _llm_canonical(d.get("sig") or {})
    if not canonical:
        return
    # CONFIRM -> WATCHING (zero risk, one tap). It is a QUESTION, not a claim. Carding it as a
    # watch is deliberate: the desk's existing _apply_entry_fill() then upgrades it to a real
    # position at the real fill price when the provider posts one, so this feeds the mechanism
    # that already exists rather than duplicating it.
    # CARD -> still only survives if the live option quote corroborates the stated premium
    #         (checked below, off the real chain — not off the model).
    payload = {"text": canonical, "provider": prov, "channel": None, "_from_llm": True,
               "_allow_test_ingest": True if os.environ.get("DESK_ENV") == "test" else None,
               "raw_override": text,
               "llm": {"action": act, "reason": d.get("reason"), "why": (r.get("why") or []),
                       "confidence": d.get("confidence"), "unresolved": d.get("unresolved"),
                       "resolved_from_context": d.get("resolved_from_context"),
                       "targets": d.get("targets"), "quotes": (d.get("llm") or {}).get("quotes"),
                       "model": LLM.MODEL},
               "force_watch": True, "needs_confirm": True}
    try:
        entry = ingest(payload)
    except Exception:
        return
    if not isinstance(entry, dict) or not entry.get("id"):
        return
    if act == "CARD":
        ok, note = LLM.corroborated(d["sig"], (entry.get("opt") or {}).get("mid"))
        entry["llm"]["corroboration"] = note
        if ok and not (entry.get("junk") or {}).get("block"):
            # An INDEPENDENT source agrees this contract trades where the provider said it does.
            entry["status"] = "NEW"
            entry["needs_confirm"] = False
        save_signals()
    _alert(f"{'🔎' if entry.get('needs_confirm') else '✅'} {prov}: "
           f"{'CONFIRM' if entry.get('needs_confirm') else 'recovered'} "
           f"{d['sig']['ticker']} {d['sig']['strike']}{d['sig']['type']}"
           f"{' @$'+str(d['sig']['premium']) if d['sig'].get('premium') else ''} "
           f"— {d.get('reason')}\n\"{text[:140]}\"",
           "Signal Desk — Chip read this")


_DRAINING = False


def drain_llm():
    """INTEGRATION POINT 3/5 — main-thread application of finished reviews. Called from the
    ingest path and from the dashboard poll, so results land within a couple of seconds without
    a second thread ever touching SIGNALS.

    Re-entrancy guard: _apply_llm_result re-enters ingest(), which calls drain_llm() again.
    That terminates on its own (the queue is popped, not peeked), but a guard is a cheaper way
    to be sure than reasoning about it on a desk that trades real money."""
    global _DRAINING
    if not LLM or _DRAINING:
        return
    _DRAINING = True
    try:
        for r in LLM.drain():
            try:
                _apply_llm_result(r)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _DRAINING = False


def ingest(data):
    text = (data.get("text") or "").strip()
    if os.environ.get("DESK_ENV") == "test" and not data.get("_allow_test_ingest"):
        return {"blocked": "test-env ingest requires _allow_test_ingest — no accidental live mutation from a test"}
    if data.get("dry_run"):                              # parse+score ONLY, never mutate SIGNALS/archive
        _res = E.analyze(text, SEG, provider=CHMAP.get(data.get("channel")) or data.get("provider"))
        return {"dry_run": True, "would_card": bool(_res), "parsed": (_res or {}).get("sig") if _res else None}
    # Channel map is authoritative (one room = one product): a single author (e.g. Prince) runs
    # BOTH a main room and the Small Account Challenge, so author-matching alone folds them together.
    # Author detection is only the fallback for channels we haven't mapped.
    prov = CHMAP.get(data.get("channel")) or E.detect_provider(data.get("author")) or data.get("provider")
    _inlog(data.get("channel"), prov, text, "RECV")   # ground-truth: this message reached the server
    # --- LLM PARSING LAYER (integration point 4/5) ---
    # Record EVERY message for conversational context before any parsing decision, so that
    # "In SMCI july 31" can later be resolved against "Watching SMCI 25 calls". Then apply any
    # reviews that finished since the last request. Both are no-ops without the module.
    if LLM and not data.get("_from_llm"):
        try:
            LLM.note_message(prov, text, msg_id=data.get("msg_id"))
        except Exception:
            pass
    drain_llm()
    if prov and E.provider_disabled(prov):            # user ELIMINATED this provider — drop it entirely
        _inlog(data.get("channel"), prov, text, "DISABLED")
        return {"disabled": prov}
    # PROVIDER EDIT: they fixed a callout (e.g. 7/13 -> 7/15). Drop the stale un-taken card so the
    # corrected text re-cards fresh (re-parsed + re-priced). Never touch a card you've already TAKEN.
    if data.get("edited") and data.get("msg_id"):
        old = next((s for s in SIGNALS if s.get("msg_id") == data.get("msg_id")), None)
        if old and old.get("status") in ("NEW", "WATCHING"):
            SIGNALS.remove(old)
            _archive_signal(prov, {**old, "status": "DELETED"})   # tombstone -> restore_signals won't resurrect it
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
        _llm_review(text, prov, res, None, c, data)         # LLM LAYER (5/5a): nothing parsed
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
        for s in reversed(SIGNALS[-SIGNALS_CAP:]):   # bounded — SIGNALS is trimmed to CAP (~2 days of cards)
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
                                  strike=res["sig"]["strike"], expiry=_exp,
                                  want_equity=(EQUITY_SOURCE == "ibkr"))   # avoid IBKR err 322 spam
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
    # CHIP (self-generated provider): inject chip's PER-TICKER R-targets into the ticket + plan BEFORE the
    # card/shadow/DB are built, so _init_shadow + _signal_row + _resync_shadow_r all score chip's shadow
    # against ITS OWN t1/t2/stop (not the generic 40/80 defaults). Guarded to provider=="chip" only.
    if prov == "chip" and data.get("chip_plan"):
        cp = data["chip_plan"]
        # Overlay the %s AND re-derive the $ ladder off the limit (overwriting only the %s left t1/t2/t3
        # at the generic 35/70/110% dollars — a card read T1 22% but $2.47 = 1.83×1.35 instead of $2.23).
        _chip_ladder(res.setdefault("ticket", {}), cp)
        res["chip_plan"] = cp                              # persisted so the wrong-expiry heal can restore it
        res.setdefault("plan", {})["stop_pct"] = cp["stop_pct"]
        res["chip"] = data.get("chip_meta") or {}          # {regime, news, stale} badge for the card
    now = datetime.now(ET)
    sid = now.strftime("%H%M%S") + "-" + res["sig"]["ticker"] + res["sig"]["strike"]
    entry = {"id": sid, "ts": now.strftime("%H:%M:%S"), "iso": now.isoformat(),
             "ts_utc": now.astimezone(timezone.utc).isoformat(),
             "provider": prov or "unknown",
             "status": "WATCHING" if (res.get("watch") or data.get("force_watch")) else "NEW",
             "msg_id": data.get("msg_id"),
             # raw_override: an LLM-recovered card is fed back through ingest as canonical text
             # ("SMCI 25C Jul 31 @1.67"), but the card must show what the PROVIDER actually
             # wrote — otherwise the desk displays a sentence no human ever posted.
             "raw": (data.get("raw_override") or text)[:200], **res}
    if data.get("llm"):
        entry["llm"] = data["llm"]
        entry["needs_confirm"] = bool(data.get("needs_confirm"))
        entry["canonical"] = text
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
    # Ref price: the provider's stated premium OR the live fillable ticket OR the live quote mid. A signal
    # with no STATED premium ("grabbing NVDA 200c here") is still perfectly shadowable off the real market,
    # and dropping it lost the trade's whole story for no reason.
    _sref = ((entry.get("sig") or {}).get("premium") or (entry.get("ticket") or {}).get("limit")
             or (entry.get("opt") or {}).get("mid"))
    if not (entry.get("junk") or {}).get("block") and _sref:
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
    _llm_review(text, prov, res, entry, c, data)    # LLM LAYER (5/5b): carded, but is it right?
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
        # NaN/Infinity (from empty IBKR quotes) is INVALID JSON — Python emits it, but the browser's
        # JSON.parse rejects it and blanks the whole dashboard. Scrub to null at the single output point.
        if isinstance(body, bytes):
            out = body
        else:
            # One-pass strict encode (allow_nan=False rejects NaN/Inf — invalid browser JSON that blanks the
            # dashboard). Quotes are scrubbed at the SOURCE so the common path never trips; the fallback
            # keeps the NaN-guard guarantee if a non-finite float ever slips through.
            try:
                out = json.dumps(body, default=str, allow_nan=False).encode()
            except ValueError:
                out = json.dumps(_scrub_nan(body), default=str).encode()
        # gzip: /api/signals is ~250 KB of highly repetitive JSON polled every 2.5s. Compressing it is a
        # ~7x cut (251 -> 36 KB) for ~2ms of CPU, and the browser decompresses natively. Threshold keeps
        # tiny replies uncompressed, where the 20-byte gzip header would cost more than it saves.
        enc = None
        if len(out) >= GZIP_MIN and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            try:
                out = gzip.compress(out, GZIP_LEVEL)
                enc = "gzip"
            except Exception:
                enc = None
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(out)))     # was absent -> forced connection-close framing
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(out)

    def do_OPTIONS(self): self._send(200, {})

    def do_GET(self):
        if self.path.startswith("/api/signals"):
            drain_llm()      # LLM LAYER: dashboard polls every ~2.5s, so a finished review is
            refresh_account()  # applied within seconds — still entirely on the main thread.
            check_alerts()
            livekeys = ("mid", "pnl_pct", "peak_pct", "iv", "delta", "spread_pct", "alert", "mismatch",
                        "spot", "entry_spot", "spot_dir")
            withlive = lambda p: {**p, "live": {k: MONITOR.get(p["id"], {}).get(k) for k in livekeys}}
            _allpos = M.load()                          # read the positions ledger ONCE per poll (was 3×)
            # ONE card per trade: attach each position (open OR closed) to its signal by journal_id
            posmap = {p["journal_id"]: withlive(p) for p in _allpos if p.get("journal_id")}
            _window = SIGNALS[-60:]
            _armed = [s for s in SIGNALS if (s.get("reentry") or {}).get("armed") and s not in _window]
            # surface the last ~24 COMPLETED chip scalps as the persistent trade-log, even past the 60-window
            _chiphist = [s for s in SIGNALS if s.get("provider") == "chip"
                         and (s.get("shadow") or {}).get("state") == "ended" and s not in _window][-CHIP_LOG_KEEP:]
            sigs = []
            for s in reversed(_window + _armed + _chiphist):
                s2 = dict(s)
                if s["id"] in posmap:
                    s2["pos"] = posmap[s["id"]]
                if s["id"] in LIVE_SIG:
                    s2["live"] = LIVE_SIG[s["id"]]          # live move% + recommendation for un-taken cards
                if s.get("provider") == "chip" and s.get("shadow"):
                    _cl = _chip_lifecycle(s)                # per-ticker scalp lifecycle for the live/log card
                    if _cl:
                        s2["chip_lifecycle"] = _cl
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
            orphans = [withlive(p) for p in _allpos
                       if p["state"] not in M.CLOSED_STATES and p.get("journal_id") not in linked]
            orphans += [withlive(p) for p in _allpos if p.get("kind") == "tracked"]   # provider-tracked shells
            _sc_cal, _sc_exit = _scorecard_cached()
            # ALL-TIME tallies over the FULL store (the feed above is windowed to the last 60 for render
            # speed; the header shows the real totals so the count isn't frozen at 60).
            _tot = {"signals": len(SIGNALS),
                    "taken": sum(1 for s in SIGNALS if s.get("status") in ("TAKEN", "CLOSED")),
                    "skipped": sum(1 for s in SIGNALS if s.get("status") in ("SKIPPED", "EXPIRED"))}
            try:
                import chip_desk as _CD
                _chip = _CD.snapshot()
            except Exception:
                _chip = None
            return self._send(200, {"account": E.ACCOUNT, "risk": E.RISK_PCT, "start": START_ACCOUNT,
                                    "now": datetime.now(ET).isoformat(),
                                    "sleeve": SLEEVE, "sources": SOURCES, "discipline": D.state(),
                                    "killed": V.killed_providers(), "day_override": _day_override_active(),
                                    "positions": orphans, "totals": _tot, "ibkr": IBKR,
                                    "pending": PENDING, "signals": sigs, "stats": ST.performance(),
                                    "calibration": _sc_cal, "exit_quality": _sc_exit,
                                    "provider_edge": _provider_edge_cached(), "chip": _chip,
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
        try:
            import chip_desk
            threading.Thread(target=chip_desk.scan_loop, daemon=True).start()
            print("🤖 chip desk started (self-generating SHADOW provider 'chip' — SPY/QQQ).")
        except Exception as _e:
            print(f"   (chip desk failed to start: {_e})")
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
