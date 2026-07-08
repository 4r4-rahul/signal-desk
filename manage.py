#!/usr/bin/env python3
"""
Trade-management layer (SME spec): classify provider follow-up messages, keep an
open-position store, link messages to positions, and record the trader's real actions.

Provider messages are INFORMATION, not orders — the app parses -> classifies -> links
-> suggests; the trader confirms with his real numbers.
"""
import re, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).parent
ET = timezone(timedelta(hours=-4))
STORE = ROOT / "channels" / "_positions.json"
KNOWN = {"SPX", "SPY", "QQQ", "IWM", "NDX", "DIA", "NVDA", "TSLA", "AAPL", "META", "AMZN",
         "MSFT", "GOOGL", "AMD", "SMCI", "SMTC", "PLTR", "COIN", "MSTR", "AVGO", "SOFI",
         "MU", "NFLX", "CRWV", "VIX", "TTD", "MRVL", "SNDK", "NOW", "BMNR", "TEM"}
now_iso = lambda: datetime.now(ET).isoformat()


FRAC = {"1/4": 25, "1/3": 33, "1/2": 50, "half": 50, "2/3": 66, "3/4": 75}


def classify(text):
    """Return {type, ticker, leg, pct_out, pct_up, price, stop_ref, estimated, raw}.
    Learned from every provider's real vocabulary (Sulker 'de-risk', Mike fractions,
    JMT bare '%', Option King 'secure profit', Prince 'letting ride')."""
    raw0 = text.strip()
    low = " " + re.sub(r"\s+", " ", raw0.lower()) + " "
    low = re.sub(r"(\d),(\d{3})", r"\1\2", low)          # 1,100% -> 1100%

    def has(*w):
        return any(x in low for x in w)

    tk = None
    m = re.search(r"\$([a-z]{1,5})\b", low)
    if m and m.group(1).upper() in KNOWN:
        tk = m.group(1).upper()
    if not tk:
        for k in KNOWN:
            if re.search(rf"\b{re.escape(k.lower())}\b", low):
                tk = k; break
    lm = re.search(r"(\d{1,4}(?:\.\d{1,2})?)\s*(c|p|call|put)\b", low)
    leg = (lm.group(1) + ("C" if lm.group(2)[0] == "c" else "P")) if lm else None

    # price-move X>Y (entry->current) -> P&L
    mv = re.search(r"(\d{1,3}\.\d{1,2})\s*[>]\s*(\d{1,3}\.\d{1,2})", low)
    pct_move = round((float(mv.group(2)) / float(mv.group(1)) - 1) * 100) if mv and float(mv.group(1)) else None

    # pct_out: explicit sell amount
    om = re.search(r"(\d{1,3})\s*%\s*(?:out|off|scaled|sold)|(?:out|off|scaled|sold|trimm?ed|selling|sell)\s*(?:some\s*)?(\d{1,3})\s*%", low)
    pct_out = int(next(g for g in om.groups() if g)) if om else None
    # pct_up: P&L status (leading %, "% up", "up %", "% here/so far/now", "runners up %")
    um = re.search(r"^\s*\+?\s*(?:runners?\s+up\s+)?(\d{1,4})\s*%|(\d{1,4})\s*%\s*up|\bup\s*(\d{1,4})\s*%|(\d{1,4})\s*%\s*(?:here|so far|now)", low.strip())
    pct_up = int(next(g for g in um.groups() if g)) if um else None
    if pct_up is None and pct_move is not None:
        pct_up = pct_move

    # fractions / word amounts -> pct_out estimate
    est = False
    fm = re.search(r"\b(1/4|1/3|1/2|2/3|3/4|half)\b", low)
    if pct_out is None and fm and has(" out", " off", "sold", "trim", "secure", "scal"):
        pct_out, est = FRAC[fm.group(1)], True
    if pct_out is None:
        if has("most out", "more than half", "sold most", "selling most", "de-risk most", "derisk most"):
            pct_out, est = 70, True
        elif has("half out", "sold half", "selling half", "trim half", "trimmed half"):
            pct_out, est = 50, True
        elif re.search(r"a little|some\b|trim|took (some|one|a few)|taking (some|one|a few)|moar out|more out|secure some", low):
            pct_out, est = 25, True

    # fill price: "at X", "@ X", after an action verb, "X now", ">Y"  (@ has no \b — it's non-word)
    pr = re.search(r"(?:\bat|@|\bout|\boff|\btrim(?:med)?|\bsold|\bscaled?|\bcut(?:ting)?|\bfilled|\bentered|\bhere)\s*\$?(\d{1,3}(?:\.\d{1,2})?)(?!\s*%)|(\d{1,3}\.\d{1,2})\s*now", low)
    price = None
    if pr:
        g = next((x for x in pr.groups() if x), None)
        if g and float(g) <= 60:
            price = float(g)
    if price is None and mv and float(mv.group(2)) <= 60:
        price = float(mv.group(2))

    # stop: premium stop (<100) or breakeven language (ignore 4-digit underlying-level stops)
    slm = re.search(r"s/?l\s*(?:set|at|to|above|up to|@)?\s*(\d{1,2}\.\d{1,2})", low)
    if slm:
        stop_ref = slm.group(1)
    elif has("s/l at entry", "stop at entry", "sl at entry", "s/l set", "b/e", "b / e", "breakeven",
             "break even", "purchase price", "free lunch", "free ride", "house money", "back to b",
             "move ur sl", "move your sl", "moving s/l", "moved s/l", "risk free", "risk-free"):
        stop_ref = "BE"
    else:
        stop_ref = None

    # ---- type resolution (precedence: exits > scale > trim > stop > status) ----
    conditional = bool(re.search(r"\b(if|might|gonna cut|will cut)\b", low)) and has("cut")
    runner = has("runner", "letting the rest ride", "let the rest ride", "letting", "rest ride",
                 "leaving a few", "leave rest", "let it ride", "ride")
    full = has("all out", "fully out", "completely out", "out rest", "out the rest", "closing rest",
               "closing the rest", "sold out", "sold the rest", "im out", "i'm out", " out here", "flat ")
    sell = (pct_out is not None) or has("de-risk", "derisk", "trim", "sold", "selling", " off", "scaled",
            "scaling", "secure", "taking some", "taking one", "took some", "took one", "moar out", "more out",
            "half out", "most out", "let ") or full

    if (has("cutting", "cut this", "i cut", "cut here", "cut rest", "cut half", "already cut", "just cut",
            "green to red", "stopped out", "stopped", "rejected", "liquidat") and not conditional):
        typ = "CUT"
    elif re.search(r"\b\d{1,3}\s*%\s*l\b", low) or has("cutting here"):
        typ = "CUT"
    elif full and runner:
        typ = "SCALE_OUT"
    elif full:
        typ = "FULL_EXIT"
    elif sell and runner:
        typ = "SCALE_OUT"
    elif sell and (pct_out is not None and pct_out >= 50):
        typ = "SCALE_OUT"
    elif sell:
        typ = "TRIM"
    elif stop_ref and not sell:
        typ = "MOVE_STOP"
    elif pct_up is not None:
        typ = "PROFIT_UPDATE"
    elif re.search(r"\b(?:in|got in|i(?:'| a)?m in)\s*(?:@|at)\s*\$?\d"
                   r"|\b(?:filled|entered|took it)\s*(?:@|at)?\s*\$?\d"
                   r"|(?:@|\bat)\s*\$?\d.{0,12}\bfill(?:ed)?\b", low) and price is not None:
        typ = "ENTRY_FILL"                              # firm fill confirmation ("IN AT 0.93", "filled 0.93")
    elif has("added", "adding", "re-enter", "re enter", "back in", "avg", "in full"):
        typ = "PROFIT_UPDATE"                            # averaging/add — log as a status note
    else:
        typ = "NOISE"

    return {"type": typ, "ticker": tk, "leg": leg, "pct_out": pct_out, "pct_up": pct_up,
            "price": price, "stop_ref": stop_ref, "estimated": est, "raw": raw0[:140]}


# ---------------- position store ----------------
def load():
    try:
        return json.loads(STORE.read_text())
    except Exception:
        return []


def save(ps):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(ps, indent=2))


def trade_key(provider, ticker, strike, typ, expiry=None):
    """Canonical identity of a trade — one card per key. Strike normalized (6141.0 -> 6141)."""
    return f"{provider}|{ticker}|{str(strike).split('.')[0]}|{(typ or '').upper()}|{expiry or '0DTE'}"


def open_position(provider, ticker, leg, entry, qty, stop, tp1, tp2, journal_id=None,
                  entry_msg_id=None, conf=None, tape=None, expiry=None):
    ps = load()
    leg = leg or ""
    p = {"id": now_iso().replace(":", "").replace(".", "")[-9:] + (ticker or "?"),
         "trade_key": trade_key(provider, ticker, leg[:-1], leg[-1:], expiry),
         "provider": provider, "ticker": ticker, "leg": leg, "entry": entry, "avg_entry": entry,
         "qty_initial": qty, "qty_remaining": qty, "stop": stop, "initial_stop": stop, "tp1": tp1, "tp2": tp2,
         "state": "OPEN", "realized": 0.0, "peak_pct": 0.0, "is_runner": False,
         "expiry": expiry, "conf_at_entry": conf, "tape_at_entry": tape,
         "provider_events": [], "user_actions": [], "live_alerts": [], "resigned_events": [],
         "opened": now_iso(), "closed": None, "journal_id": journal_id, "suggested": None,
         "entry_msg_id": entry_msg_id, "provider_pct_out": 0}   # for reply-linking + divergence
    ps.append(p); save(ps)
    return p


CLOSED_STATES = ("CLOSED", "CUT", "TRACKED")   # TRACKED = provider-followed shell, not a live trade


def open_positions():
    return [p for p in load() if p["state"] not in CLOSED_STATES]


def find_open_by_key(key):
    return next((p for p in load() if p["state"] not in CLOSED_STATES and p.get("trade_key") == key), None)


def note_repost(pid, raw):
    """A provider re-posted/edited an already-open entry — log it, don't spawn a 2nd card."""
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p:
        return None
    p.setdefault("resigned_events", []).append({"ts": now_iso(), "raw": (raw or "")[:140]})
    save(ps)
    return p


# ================= CROSS-DAY REPLY-TAGGING =================
# reply_to carries the FULL TEXT of the parent message (author header + content), NOT an id.
# So we re-parse the parent signal out of the reply and match by trade_key — works across days.

def strip_reply_header(reply_to):
    """Drop the leading author/role header lines of a reply preview, keep the signal content."""
    if not reply_to:
        return None
    lines = [l.strip() for l in reply_to.replace("\xa0", " ").splitlines() if l.strip()]
    body, header = [], True
    for l in lines:
        if header and (l.lower().startswith("server tag") or (len(l) < 24 and not re.search(r"\d", l))):
            continue
        header = False
        body.append(l)
    return "\n".join(body) if body else reply_to


def reply_parent_key(reply_to, provider):
    """Parse the parent signal out of a reply_to preview -> (trade_key, sig) or (None, None)."""
    import signal_engine as E
    body = strip_reply_header(reply_to)
    if not body:
        return None, None
    sig = E.parse_signal(body, provider=provider)
    if not sig or not sig.get("ticker") or not sig.get("strike"):
        return None, None
    prov = provider or sig.get("provider")
    return trade_key(prov, sig["ticker"], sig["strike"], sig["type"], None), sig


def find_card_by_key(key, signals):
    """Locate a card by trade_key across open positions, existing tracked shells, AND the
    persisted SIGNALS feed (may hold a card from a prior day). Returns ('pos',p)|('sig',s)|(None,None)."""
    p = find_open_by_key(key)
    if p:
        return "pos", p
    t = next((x for x in load() if x.get("kind") == "tracked" and x.get("trade_key") == key), None)
    if t:                                        # attach to the existing tracked shell, don't spawn another
        return "pos", t
    for s in reversed(signals or []):
        sg = s.get("sig") or {}
        if not sg.get("ticker"):
            continue
        if trade_key(s.get("provider"), sg.get("ticker"), sg.get("strike"), sg.get("type"), None) == key:
            return "sig", s
    return None, None


def reply_already_logged(pid, raw):
    """Dedupe: same reply text already on this card's provider_events?"""
    p = next((x for x in load() if x["id"] == pid), None)
    if not p:
        return False
    tail = (raw or "").strip()[:140]
    return any((e.get("raw") or "")[:140] == tail for e in p.get("provider_events", []))


def attach_to_position(pid, c, source=None):
    """Write a resolved-by-key provider update onto a position (provider_events + suggested)."""
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p:
        return None
    ev = {"ts": now_iso(), **c}
    if source:
        ev["source"] = source
    p["provider_events"].append(ev)
    if c["type"] in ("SCALE_OUT", "FULL_EXIT"):
        p["provider_pct_out"] = 100 if c["type"] == "FULL_EXIT" else max(p.get("provider_pct_out", 0), c.get("pct_out") or 0)
    p["suggested"] = {"type": c["type"], "hint": ACTION_HINT.get(c["type"]), "pct_out": c.get("pct_out"),
                      "price": c.get("price"), "stop_ref": c.get("stop_ref"),
                      "estimated": c.get("estimated", False), "raw": c.get("raw", "")}
    save(ps)
    return p


def attach_to_signal_card(sig_entry, c):
    """Stamp a provider update onto a NEW (not-taken) SIGNALS card, in place."""
    tail = (c.get("raw") or "")[:140]
    pe = sig_entry.setdefault("provider_events", [])
    if not any((e.get("raw") or "")[:140] == tail for e in pe):
        pe.append({"ts": now_iso(), **c, "source": "reply"})
    sig_entry["last_provider_update"] = {"ts": now_iso(), **c}


def open_tracked_from_reply(provider, psig, c, reply_to):
    """Seed a lightweight PROVIDER-TRACKED shell when we never saw the original entry.
    Not a live trade — qty 0, no journal, excluded from monitor/sizing — just a home
    for the provider's updates so the user sees his progress on a trade he didn't take."""
    leg = f"{psig['strike']}{psig['type']}"
    p = open_position(provider, psig["ticker"], leg, entry=psig.get("premium") or 0, qty=0,
                      stop=None, tp1=None, tp2=None, journal_id=None, entry_msg_id=None,
                      conf=None, tape=None, expiry=psig.get("dte"))
    ps = load()
    tp = next(x for x in ps if x["id"] == p["id"])
    tp["kind"] = "tracked"
    tp["state"] = "TRACKED"
    tp["seeded_from"] = (reply_to or "")[:200]
    tp["provider_events"].append({"ts": now_iso(), **c, "source": "reply"})
    if c["type"] in ("SCALE_OUT", "FULL_EXIT"):
        tp["provider_pct_out"] = 100 if c["type"] == "FULL_EXIT" else (c.get("pct_out") or 0)
    save(ps)
    return tp


def prune_stale(today_iso):
    """Drop yesterday's 0DTE tracked/closed shells; keep live trades and dated (multi-day) expiries."""
    ps, keep, today = load(), [], today_iso[:10]
    for p in ps:
        if p["state"] in ("OPEN", "RUNNER", "PARTIALLY_TRIMMED"):
            keep.append(p); continue
        exp = p.get("expiry")
        dated = isinstance(exp, str) and len(exp) >= 8
        if p.get("kind") == "tracked" and not dated and (p.get("opened") or "")[:10] < today:
            continue
        keep.append(p)
    save(keep)


def log_live_alert(pid, text, peak=None):
    """Persist a state-changing live-market alert onto the position (for the lifetime record)."""
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p:
        return
    la = p.setdefault("live_alerts", [])
    if la and la[-1].get("alert") == text:
        return
    la.append({"ts": now_iso(), "alert": text})
    if peak is not None:
        p["peak_pct"] = max(p.get("peak_pct", 0), round(peak, 1))
    save(ps)


def update_peak(pid, peak):
    """Persist a new peak so the lifetime view has the market high even if TWS later drops."""
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p:
        return
    if round(peak, 1) > p.get("peak_pct", 0) + 2:
        p["peak_pct"] = round(peak, 1)
        save(ps)


ACTION_HINT = {"TRIM": "Trim", "SCALE_OUT": "Scale out (keep runner)", "FULL_EXIT": "Close all",
               "CUT": "⚠ CUT (loss)", "MOVE_STOP": "Move stop", "PROFIT_UPDATE": None, "RUNNER_UPDATE": None}


def _env(k):
    f = ROOT / ".env"
    for ln in (f.read_text().splitlines() if f.exists() else []):
        if ln.startswith(k + "="):
            return ln.split("=", 1)[1].strip()
    return None


def classify_llm(text):
    """LLM fallback (Claude) for messy messages the regex can't classify. Returns same schema."""
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        return None
    import urllib.request
    prompt = ("You classify options-trading follow-up messages for a copy-trading app. "
              "Return ONLY a JSON object: {\"type\": one of TRIM|SCALE_OUT|FULL_EXIT|CUT|MOVE_STOP|"
              "PROFIT_UPDATE|NOISE, \"ticker\": string or null, \"leg\": string or null, "
              "\"pct_out\": int or null, \"pct_up\": int or null, \"price\": number or null, "
              "\"stop_ref\": \"BE\" or a number or null}. "
              "TRIM=small partial sell, SCALE_OUT=big partial keeping a runner, FULL_EXIT=close all, "
              "CUT=exit at a loss, MOVE_STOP=adjust stop, PROFIT_UPDATE=just a P&L status, "
              "NOISE=market commentary with no action.\nMessage: " + repr(text))
    body = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 150,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        m = re.search(r"\{.*\}", r["content"][0]["text"], re.S)
        c = json.loads(m.group(0)) if m else None
        if c:
            c.setdefault("raw", text[:140]); c.setdefault("leg", None)
            c.setdefault("estimated", True); c["via_llm"] = True
        return c
    except Exception:
        return None


def assign_to_position(pid, c):
    """Attach an already-classified action to a chosen position (disambiguation resolve)."""
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p:
        return None
    p["provider_events"].append({"ts": now_iso(), **c})
    if c["type"] in ("SCALE_OUT", "FULL_EXIT"):
        p["provider_pct_out"] = 100 if c["type"] == "FULL_EXIT" else max(p.get("provider_pct_out", 0), c.get("pct_out") or 0)
    p["suggested"] = {"type": c["type"], "hint": ACTION_HINT.get(c["type"]), "pct_out": c.get("pct_out"),
                      "price": c.get("price"), "stop_ref": c.get("stop_ref"),
                      "estimated": c.get("estimated", False), "raw": c.get("raw", "")}
    save(ps)
    return p


def link_and_attach(msg):
    """Attach a classified management msg to the right open position (same provider)."""
    c = msg.get("classified") or classify(msg.get("text", ""))
    prov = msg.get("provider")
    reply_to = msg.get("reply_to")
    if c["type"] == "NOISE" or not prov:
        return None
    ps = load()
    opens = [p for p in ps if p["state"] not in ("CLOSED", "CUT") and p["provider"] == prov]
    if not opens:
        return None
    cand = None
    # 1) REPLY-REFERENCE — provider tagged the original entry msg: exact, no guessing
    if reply_to:
        cand = next((p for p in opens if p.get("entry_msg_id") and p["entry_msg_id"] == reply_to), None)
    # 2) ticker (+ leg) match
    if not cand and c["ticker"]:
        by_tk = [p for p in opens if p["ticker"] == c["ticker"]]
        if c["leg"]:
            exact = [p for p in by_tk if p["leg"] == c["leg"]]
            cand = exact[0] if exact else (by_tk[-1] if by_tk else None)
        else:
            cand = by_tk[-1] if by_tk else None
    # 3) no ticker & no reply -> only auto-link if a single position is open
    if not cand:
        cand = opens[-1] if len(opens) == 1 else None
    if not cand:
        # ambiguous -> needs disambiguation (multiple opens, no reply/ticker)
        return {"unlinked": True, "classified": c,
                "candidates": [{"id": p["id"], "ticker": p["ticker"], "leg": p["leg"], "state": p["state"]}
                               for p in opens]}
    cand["provider_events"].append({"ts": now_iso(), **c})
    if c["type"] in ("SCALE_OUT", "FULL_EXIT"):
        cand["provider_pct_out"] = 100 if c["type"] == "FULL_EXIT" else max(cand.get("provider_pct_out", 0), c["pct_out"] or 0)
    cand["suggested"] = {"type": c["type"], "hint": ACTION_HINT.get(c["type"]),
                         "pct_out": c["pct_out"], "price": c["price"], "stop_ref": c["stop_ref"],
                         "estimated": c["estimated"], "raw": c["raw"]}
    save(ps)
    return {"position": cand["id"], "classified": c}


def apply_action(pid, kind, qty=None, price=None, stop=None):
    """Record the trader's REAL action. kind: SELL | STOP | CLOSE | CUT | RUNNER_KEEP.
    Every action is timestamped into user_actions[] so the whole trade is replayable."""
    ps = load()
    p = next((x for x in ps if x["id"] == pid), None)
    if not p:
        return None
    act = {"ts": now_iso(), "kind": kind}
    if kind == "STOP" and stop is not None:
        p["stop"] = float(stop)
        act["stop"] = float(stop)
    elif kind == "RUNNER_KEEP":                      # "leave it" — an explicit, logged hold
        p["is_runner"] = True
        if p["state"] not in ("CLOSED", "CUT"):
            p["state"] = "RUNNER"
        act["note"] = "kept as runner"
    else:
        q = int(qty) if (qty and kind == "SELL") else p["qty_remaining"]
        q = min(q, p["qty_remaining"])
        fill = float(price) if price is not None else p["avg_entry"]
        delta = round((fill - p["avg_entry"]) * q * 100, 2)
        p["realized"] = round(p["realized"] + delta, 2)
        p["qty_remaining"] -= q
        act.update(qty=q, price=fill, realized_delta=delta)
        if p["qty_remaining"] <= 0:
            p["state"] = "CUT" if (kind == "CUT" or p["realized"] < 0) else "CLOSED"
            p["closed"] = now_iso()
        elif kind == "SELL":
            p["state"] = "RUNNER" if p["qty_remaining"] <= p["qty_initial"] * 0.4 else "PARTIALLY_TRIMMED"
    p.setdefault("user_actions", []).append(act)
    # auto-nudge: after banking a partial, protect the runner -> move stop to breakeven
    if p["state"] in ("RUNNER", "PARTIALLY_TRIMMED") and (p["stop"] is None or p["stop"] < p["avg_entry"]):
        p["suggested"] = {"type": "MOVE_STOP", "hint": "Move stop to breakeven (protect the win)",
                          "stop_ref": round(p["avg_entry"], 2), "pct_out": None, "price": None,
                          "estimated": False, "raw": "auto: you banked a partial — don't let the runner turn red"}
    else:
        p["suggested"] = None
    if p["state"] in ("CLOSED", "CUT"):              # trade over -> journal + full lifetime record
        _to_journal(p)
        p["close_record"] = _build_close_record(p)
        _write_close_record(p["close_record"], p.get("provider"))
    save(ps)
    return p


def _build_close_record(p):
    """The ENTIRE lifetime of a trade, stored on close (feeds expectancy + optimal-exit study)."""
    leg = p.get("leg") or ""
    cost = (p.get("avg_entry") or 0) * p.get("qty_initial", 0) * 100
    opened, closed = p.get("opened"), p.get("closed") or now_iso()
    hold = None
    try:
        hold = int((datetime.fromisoformat(closed) - datetime.fromisoformat(opened)).total_seconds())
    except Exception:
        pass
    ret = round(p["realized"] / cost * 100, 1) if cost else 0.0
    risk0 = abs((p.get("avg_entry", 0) - (p.get("initial_stop") or 0)) * p.get("qty_initial", 0) * 100) or (cost * 0.45)
    fills = [a for a in p.get("user_actions", []) if a.get("qty")]
    peak = p.get("peak_pct")

    def _utc(t):                                        # ET-offset iso -> explicit UTC iso
        try:
            return datetime.fromisoformat(t).astimezone(timezone.utc).isoformat()
        except Exception:
            return None

    def _utc_ev(events):                                # stamp ts_utc onto each timed event
        out = []
        for e in events or []:
            e2 = dict(e)
            if e.get("ts"):
                e2["ts_utc"] = _utc(e["ts"])
            out.append(e2)
        return out
    return {
        "trade_key": p.get("trade_key"), "journal_id": p.get("journal_id"),
        "provider": p.get("provider"), "ticker": p.get("ticker"), "leg": leg,
        "strike": leg[:-1], "type": leg[-1:], "expiry": p.get("expiry"),
        "opened": opened, "closed": closed, "opened_utc": _utc(opened), "closed_utc": _utc(closed),
        "hold_secs": hold, "state": p["state"],
        "qty_initial": p.get("qty_initial"), "avg_entry": p.get("avg_entry"),
        "initial_stop": p.get("initial_stop"), "tp1": p.get("tp1"), "tp2": p.get("tp2"),
        "conf_at_entry": p.get("conf_at_entry"), "tape_at_entry": p.get("tape_at_entry"),
        "entry_greeks": p.get("entry_greeks"), "entry_stop": p.get("entry_stop"),   # signal snapshot at entry
        "origin": p.get("origin"),                     # the FULL origin signal card — entire arc, one record
        "realized": p["realized"], "return_pct": ret,
        "R_multiple": round(p["realized"] / risk0, 2) if risk0 else None,
        "peak_pct": peak, "exit_vs_peak_pct": (round(ret - peak, 1) if peak else None),
        "provider_pct_out_final": p.get("provider_pct_out", 0), "your_pct_out_final": 100,
        "fills": _utc_ev(fills), "provider_events": _utc_ev(p.get("provider_events", [])),
        "live_alerts": _utc_ev(p.get("live_alerts", [])), "user_actions": _utc_ev(p.get("user_actions", [])),
    }


def _write_close_record(rec, provider):
    try:
        d = ROOT / "channels" / (provider or "unknown")
        d.mkdir(parents=True, exist_ok=True)
        with (d / "closed_trades.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _to_journal(p):
    """On full close, update the tracker journal row so it feeds sleeve/discipline/validate."""
    try:
        import tracker as T
        if not p.get("journal_id"):
            return
        jp = T.paths(p["provider"])[1]
        j = T.read_journal(jp)
        row = j.get(p["journal_id"])
        if row:
            cost = p["avg_entry"] * p["qty_initial"] * 100
            row["my_return_pct"] = f"{(p['realized'] / cost * 100):.1f}" if cost else "0"
            row["status"] = "CLOSED"
            T.write_journal(jp, j)
    except Exception:
        pass


if __name__ == "__main__":
    for t in ["all out 80% leaving runners $SPY 606C", "20% up, 50% out", "S/l above b/e",
              "$GOOGL 200C went green to red, i am cutting this one", "trimmed most part",
              "560 is major gamma level", "$TSLA 80% out at 4.2", "25% up closing the rest"]:
        c = classify(t)
        print(f"{c['type']:<13} tk={c['ticker']} leg={c['leg']} out={c['pct_out']} up={c['pct_up']} "
              f"px={c['price']} stop={c['stop_ref']} | {t[:45]}")
