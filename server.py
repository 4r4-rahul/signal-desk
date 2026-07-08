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
ENRICH = os.environ.get("ENRICH_IBKR", "1") == "1"   # live technicals from IBKR
START_ACCOUNT = E.ACCOUNT                              # the strategy allocation (env ACCOUNT)
# compounding source: allocation sleeve (default), full ibkr balance, or fixed
EQUITY_SOURCE = os.environ.get("EQUITY_SOURCE", "allocation")

PORT = 8787


SLEEVE = {"equity": START_ACCOUNT, "hwm": START_ACCOUNT, "drawdown": 0.0, "throttle": 1.0, "halted": False}
ALERTED = {"killed": set()}


def touch_source(key, provider=None, desc=None, display=None):
    """Register/update a listening channel (keyed by unique channel id): heartbeat + stats."""
    now = datetime.now(ET)
    s = SOURCES.setdefault(key, {"name": display or key, "count": 0, "first": now.isoformat()})
    s["last_beat"] = now.isoformat()
    if display:
        s["name"] = display
    if provider:
        s["provider"] = provider
    if desc:
        s["count"] += 1
        s["last_signal"] = desc
        s["last_signal_iso"] = now.isoformat()


def _alert(text, title):
    try:
        N.send_alert(text, title)
    except Exception:
        pass


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
LIVE_SIG = {}   # card_id -> live quote + move% + recommendation for an un-taken actionable card
DAY_OVERRIDE_DATE = None   # ISO date the day-stop is manually overridden (auto-resets each day)


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


def _pos_alert(p, st, key, text):
    if key in st["fired"]:
        return
    st["fired"].add(key)
    st["alert"] = text
    M.log_live_alert(p["id"], text, st.get("peak_pct"))     # persist for the lifetime record
    _alert(f"{p['provider']} {p['ticker']} {p['leg']}: {text}  (live ${st.get('mid')})",
           "Signal Desk — LIVE EXIT")


def _check_pos_rules(p, st):
    from datetime import datetime
    pnl, peak = st.get("pnl_pct", 0), st.get("peak_pct", 0)
    if pnl <= -45:
        _pos_alert(p, st, "stop", "🛑 STOP −45% hit — exit now")
    elif pnl >= 40 and p["state"] == "OPEN":
        _pos_alert(p, st, "tp1", "🎯 +40% — scale out / take profits")
    if p["state"] == "RUNNER" and peak >= 30 and pnl <= peak - 25:
        _pos_alert(p, st, "trail", f"📉 runner gave back — was +{peak:.0f}%, now +{pnl:.0f}% — exit")
    hhmm = datetime.now(ET).hour * 100 + datetime.now(ET).minute
    if hhmm >= 1545:
        _pos_alert(p, st, "eod", "⏰ EOD — flat 0DTE before the close")


def monitor_loop():
    import market as MK
    conn = None
    while True:
        try:
            if conn is None:
                conn = MK.connect(client_id=80)
                try: conn.reqMarketDataType(int(MK.env("IBKR_MARKET_DATA_TYPE", "2")))
                except Exception: pass
            for p in M.open_positions():
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
                pnl = (q["mid"] / entry - 1) * 100 if entry else 0
                st.update(expiry=q.get("expiry", st.get("expiry")), mid=q["mid"], pnl_pct=round(pnl, 1),
                          iv=q.get("iv"), delta=q.get("delta"), spread_pct=q.get("spread_pct"))
                st["peak_pct"] = round(max(st.get("peak_pct", pnl), pnl), 1)
                M.update_peak(p["id"], st["peak_pct"])       # persist the market high
                _check_pos_rules(p, st)
                conn.sleep(1)
            # live-quote the ACTIONABLE un-taken cards -> live move% + recommendation on the card
            act = [s for s in SIGNALS if s.get("status") in ("NEW", "WATCHING") and s.get("verified")
                   and (not s.get("paper") or s.get("manual_override")) and (s.get("sig") or {}).get("premium")]
            for s in act[-12:]:                              # cap: 12 most-recent actionable cards
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
                    prev = LIVE_SIG.get(s["id"], {}).get("level")
                    green = {"go", "better"}
                    if r["level"] in green and prev not in green:      # flipped INTO the buy zone -> ping once
                        tk = s.get("ticket") or {}
                        leg = f'{sg["ticker"]} {sg["strike"]}{sg["type"]}'
                        zone = "buy zone" if r["level"] == "go" else "below signal (cheaper)"
                        _alert(f"✅ {s['provider']}: {leg} entered the {zone} ({r['chase_pct']:+d}% vs signal) — "
                               f"enter ~${q.get('ask') or r['mid']}, set stop ${tk.get('stop', '?')} · "
                               f"T1 ${tk.get('t1','?')} / T2 ${tk.get('t2','?')}", "Signal Desk — Buy zone ✅")
                    LIVE_SIG[s["id"]] = r
                conn.sleep(1)
            for k in list(LIVE_SIG):                          # drop stale ids no longer in the feed
                if k not in {s["id"] for s in SIGNALS}:
                    LIVE_SIG.pop(k, None)
            conn.sleep(20)
        except Exception:
            try: conn.disconnect()
            except Exception: pass
            conn = None
            import time; time.sleep(10)


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


def ingest(data):
    text = (data.get("text") or "").strip()
    # Channel map is authoritative (one room = one product): a single author (e.g. Prince) runs
    # BOTH a main room and the Small Account Challenge, so author-matching alone folds them together.
    # Author detection is only the fallback for channels we haven't mapped.
    prov = CHMAP.get(data.get("channel")) or E.detect_provider(data.get("author")) or data.get("provider")
    _inlog(data.get("channel"), prov, text, "RECV")   # ground-truth: this message reached the server
    refresh_account()                       # COMPOUND: size off current sleeve equity

    # management follow-up? (has an action verb) -> attach to an open position, not a new card
    c = M.classify(text)
    if c["type"] == "NOISE" and prov and _looks_mgmt(text) and M.open_positions():
        c2 = M.classify_llm(text)                     # LLM fallback for messy messages
        if c2 and c2.get("type") not in (None, "NOISE"):
            c = c2

    # A FRESH ENTRY wins over the management classifier: providers put stops ("s/l 1.20") IN their
    # entries, which would otherwise look like MOVE_STOP. Only a sell/exit verb marks a real update.
    res = E.analyze(text, SEG, provider=prov)
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
        _inlog(data.get("channel"), prov, text, "REJECT")   # not a parseable signal (or commentary)
        return None
    # DE-DUPE — ONE card per trade (trade_key = provider|ticker|strike|type|expiry).
    now = datetime.now(ET)
    sg = res["sig"]
    key = M.trade_key(prov, sg["ticker"], sg["strike"], sg["type"], None)
    #  a) a re-post/edit of an already-OPEN trade (any age) -> log it, keep the one card
    op = M.find_open_by_key(key)
    if op:
        M.note_repost(op["id"], text)
        return {"duplicate": op.get("journal_id") or op["id"], "merged": True}
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
    for s in reversed(SIGNALS[-120:]):
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
                # re-size with the SAME provider multiplier + conviction as analyze() — else the
                # tape-adjusted card silently reverts to full-trust sizing (the leak).
                _pv = res["sig"].get("provider")
                _conv = E.conviction_factor(s - E.trust_of(_pv))
                res["conviction"] = _conv
                _nowmin = max(0, (datetime.now(ET).hour * 60 + datetime.now(ET).minute) - 570)  # min since 9:30 ET
                _rp = round(E.size_pct(s, res["sig"]["lotto"], E.provider_size_mult(_pv),
                                       res["sig"].get("premium"), base_trust=E.trust_of(_pv)) * E.THROTTLE, 3)
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
                # SETUP GRADE + quality-based sizing: award MORE contracts to the best-executed setups
                # (better R:R / tighter spread / buy-zone), bounded — plan_data's hard caps still apply.
                gr = E.setup_grade(res.get("ticket"), res.get("tier_key"), res["sig"].get("lotto"),
                                   (res.get("ticket") or {}).get("chasing_pct") or 0)
                res["grade"] = gr
                if res.get("plan", {}).get("ok") and not res["plan"].get("manual") and gr["mult"] != 1.0:
                    res["plan"] = E.plan_data(res["sig"], round(_rp * gr["mult"], 3), conviction=_conv,
                                              q=tech.get("opt"), ind=tech.get("ind"), rv=tech.get("rv"),
                                              now_min=_nowmin)
        except Exception:
            pass
    now = datetime.now(ET)
    sid = now.strftime("%H%M%S") + "-" + res["sig"]["ticker"] + res["sig"]["strike"]
    entry = {"id": sid, "ts": now.strftime("%H:%M:%S"), "iso": now.isoformat(),
             "ts_utc": now.astimezone(timezone.utc).isoformat(),
             "provider": prov or "unknown",
             "status": "WATCHING" if res.get("watch") else "NEW",   # provider eyeing it, not filled yet
             "msg_id": data.get("msg_id"), "raw": text[:200], **res}
    SIGNALS.append(entry)
    save_signals()
    _inlog(data.get("channel"), prov, text, "CARD")
    _archive_signal(prov, entry)                    # permanent structured archive of every signal
    if entry.get("paper") and prov:                 # B4: log paper-provider entries for graduation
        _log_paper_entry(prov, sid, entry)
    touch_source(data.get("channel") or prov or "unknown", prov,
                 f"{res['sig']['ticker']} {res['sig']['strike']}{res['sig']['type']}", data.get("name"))
    # PING on an actionable, tradeable signal (verified real, not a watch, not paper-without-override,
    # decent tier) — with the ready-to-key order ticket so you can place it immediately.
    tk = res.get("ticket")
    if (tk and entry.get("verified") and not entry.get("watch")
            and (not entry.get("paper") or entry.get("manual_override"))
            and res.get("tier_key") in ("HIGH", "MEDIUM")):
        leg = f"{res['sig']['ticker']} {res['sig']['strike']}{res['sig']['type']}"
        _alert(f"🎯 {prov}: {leg} — LIMIT ${tk['limit']} · stop ${tk['stop']} · "
               f"T1 ${tk['t1']} / T2 ${tk['t2']}" + (f" · R:R {tk['rr1']}×→{tk['rr2']}×" if tk.get('rr2') else ""),
               "Signal Desk — Take?")
    # terminal echo
    print(f"\n⚡ {entry['ts']} {prov or '?'}  {res['sig']['ticker']} {res['sig']['strike']}"
          f"{res['sig']['type']}  conf {res['score']}/100 {res['tier_key']}", flush=True)
    return entry


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
            M.open_position(prov, sig.get("ticker"), f"{sig.get('strike','')}{sig.get('type','')}",
                            float(row["my_entry"] or 0) or sig.get("premium"),
                            qty, (pl or {}).get("stop"),
                            (pl or {}).get("tp1"), (pl or {}).get("tp2"), journal_id=sid,
                            entry_msg_id=entry.get("msg_id"),
                            conf=entry.get("score"), tape=(entry.get("tech") or {}).get("label"),
                            expiry=(_resolve_expiry(sig) or (entry.get("opt") or {}).get("expiry")))
            # stamp the FULL ORIGIN SIGNAL CARD onto the position -> the closed trade becomes ONE
            # self-contained record of the entire arc (signal + recommendation + Greeks at entry).
            _ps = M.load()
            for _pp in _ps:
                if _pp.get("journal_id") == sid:
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
    journal[sid] = row
    T.write_journal(jp, journal)
    entry["status"] = row["status"]
    save_signals()
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
            livekeys = ("mid", "pnl_pct", "peak_pct", "iv", "delta", "spread_pct", "alert")
            withlive = lambda p: {**p, "live": {k: MONITOR.get(p["id"], {}).get(k) for k in livekeys}}
            # ONE card per trade: attach each position (open OR closed) to its signal by journal_id
            posmap = {p["journal_id"]: withlive(p) for p in M.load() if p.get("journal_id")}
            sigs = []
            for s in reversed(SIGNALS[-60:]):
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
            return self._send(200, {"account": E.ACCOUNT, "risk": E.RISK_PCT, "start": START_ACCOUNT,
                                    "now": datetime.now(ET).isoformat(),
                                    "sleeve": SLEEVE, "sources": SOURCES, "discipline": D.state(),
                                    "killed": V.killed_providers(), "day_override": _day_override_active(),
                                    "positions": orphans,
                                    "pending": PENDING, "signals": sigs, "stats": ST.performance()})
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
        if self.path.startswith("/api/action"):
            return self._send(200, act(data))
        if data.get("watching"):                    # heartbeat from a watching window
            touch_source(data.get("channel") or "?", CHMAP.get(data.get("channel")), display=data.get("name"))
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
        print("📡 Live position monitor started (IBKR option prices + Greeks).")
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
