#!/usr/bin/env python3
"""
MESSAGE ROUTER + CONVERSATIONAL CONTEXT ENGINE for the live options desk.

WHY THIS EXISTS
    The desk routes every inbound message by keyword heuristics into CARD / MGMT / REJECT.
    Over 706 real messages that split 204 / 199 / 231 — nearly a THIRD of the feed is management
    of a position that already exists. Keyword routing gets that wrong in both directions, and the
    two directions cost very different amounts:

      FABRICATION (expensive).  "303% on $SMCI calls ... My price target for today is $32.16"
        was routed to CARD and became an SMCI 32.16C — a strike that does not exist, on a board a
        human is about to key into tastytrade. The message is a P&L UPDATE about a position that
        was already open. Nothing new should have appeared.

      MISS (cheap).  10:14 "Watching $PLTR $134 calls ... entry around 2.05" -> carded as a watch.
        10:29 "Getting in PLTR here @2.03" -> REJECT. The desk carded the setup and threw away the
        actual fill. A human can see that in the log; nobody trades a contract that does not exist.

    So this module has one job and one asymmetry.

THE ASYMMETRY (the core safety property — everything else is detail)
    DEMOTING is cheap. Turning a card back into an update or into noise makes the board QUIETER.
    The model may do that on its own judgement, because the worst case is a missed trade that is
    still sitting in the log.

    PROMOTING is expensive. A model that is confident and wrong must NOT be able to put a contract
    on the board. So a promotion is not applied directly: it is rewritten into canonical text and
    re-entered through server.ingest() from the top, where quote grounding has already run and
    where contract verification, the price-mismatch guard, the junk gate, dedup and sizing all
    still run exactly as they do for a regex parse. The model can only ever ASK for a card.

    Consequence, stated plainly: a confident-but-wrong model can make this desk quieter. It cannot
    make it louder with contracts that do not exist.

THE HONESTY CONTRACT (the year fix, generalised)
    The model reports what the provider SAID. Deterministic code decides what it MEANS.
      - The YEAR of an expiry is never taken from the model (llm_parse._strip_year; measured:
        haiku returned 2025-07-31 in 2026). server._resolve_expiry derives it.
      - WHICH open position a bare "out half" refers to is never taken from the model either. The
        model may say "this refers to something earlier"; the deterministic Book below decides
        which one, and if more than one fits it returns AMBIGUOUS and the desk asks.
      - A class is only allowed to produce a contract if the class is contract-producing. A
        PNL_UPDATE cannot card, no matter what fields the model filled in.

SPEED
    ingest() must not touch the network. Routing happens in three layers:
      L0  deterministic prefilter — free, synchronous, catches the trivially-obvious both ways.
      L1  the existing regex route — unchanged, synchronous, decides what the desk shows NOW.
      L2  the LLM — asynchronous on a worker thread, and may later CORRECT L1.
    on_message() does L0 + an enqueue and returns. Measured added ingest latency: see
    `python3 llm_eval.py route-latency`.

FAIL OPEN
    No API key, no SDK, timeout, rate limit, malformed response, module missing, adapter not bound
    -> every function here becomes a no-op and the desk behaves byte-identically to today.
"""
import json, os, queue, re, threading, time
from collections import defaultdict, deque
from pathlib import Path

import deskpaths as _P

try:
    import llm_parse as LP            # shared client, breaker, budget, grounding, context window
except Exception:                     # module missing -> the whole router is inert
    LP = None

MODEL = "claude-haiku-4-5"
# WHY HAIKU 4.5 FOR ROUTING: this is single-label classification over <2 KB of text against a
# fixed 20-way label set with a strict tool schema. That is the cheapest possible shape of LLM
# work and the tier Haiku exists for. $1/$5 per MTok against Opus 4.8's $5/$25 and Sonnet 5's
# $3/$15. The accuracy we could buy by escalating is accuracy we cannot bank: a promotion still
# has to clear grounding + market corroboration + the junk gate, and a demotion is safe by
# construction, so a smarter model moves the ASK rate, not the fabrication rate. It is also
# already the model llm_parse and manage.classify_llm use, so the desk has ONE model to reason
# about. At ~35-54 messages/day the whole layer costs well under $0.30/day either way — model
# choice here is a safety and latency decision, not a cost one.
#
# PROMPT CACHING: deliberately NOT used, and this is a MEASURED decision, not an omission. The
# minimum cacheable prefix on Haiku 4.5 is 4096 tokens. Measured prompts on this corpus run
# ~2200-2800 tokens including the system prompt and tool schema, i.e. BELOW that floor, so a
# cache_control marker is silently ignored — it would look like an optimisation in the diff and
# do nothing. Confirmed empirically: cache_creation_input_tokens and cache_read_input_tokens are
# both 0 on every call. `python3 llm_eval.py route-live` prints those counters.
MAX_TOKENS = 600
QUEUE_MAX = 64
CONF_DEMOTE = 0.60        # below this, even a demotion is just noise — leave the desk alone
CONF_PROMOTE = 0.75       # a promotion still has to clear the deterministic guards afterwards

_CTX_FILE = _P.CHANNELS / "_route_context.json"

# ---------------------------------------------------------------- the taxonomy
# The parallel agent owns eval/message_taxonomy.md. These are the classes this router is built
# against; _load_taxonomy() below reconciles with that file when it lands so the two cannot drift
# silently. Unknown classes from the file are surfaced, never silently dropped.

CLASSES = [
    "ENTRY",        # in, or entering now, and THIS message fully specifies the contract
    "WATCH",        # a named setup they are NOT in: eyeing, resting a limit, "looks nice"
    "FILL",         # a previously-watched setup is now filled. Usually anaphoric.
    "ADD",          # scale-in / average-down on an existing position
    "TRIM",         # selling part of an existing position
    "EXIT",         # closing the whole position (target, stop, or voluntary)
    "STOP_SET",     # setting a stop on an open position
    "STOP_MOVE",    # moving an existing stop
    "TARGET_SET",   # stating or moving a profit target
    "PNL_UPDATE",   # reporting % or $ on an existing position. NEVER mints a contract.
    "RECAP",        # end-of-day/weekly summary listing several CLOSED trades
    "COMMENTARY",   # market view, level talk, chart narration, plan-for-the-day
    "WARNING",      # risk caution: "manage ur risk", "go small", "it is risky"
    "QUESTION",     # a question to the room
    "CHAT",         # banter, off-topic
    "NOISE",        # URL-only, GIF, emoji, empty, test
    "EDIT",         # a Discord edit of a message already posted
    "REPOST",       # the scraper replayed a message already ingested
    "NON_PROVIDER", # not from a trading provider (bot/ops feed, unmapped channel)
    "MUST_ASK",     # an action is clearly intended but the referent/a required field is
                    # unrecoverable. Do not card, do not guess — ask.
]
# Aligned with eval/message_taxonomy.md §2.1 (owned by the parallel agent). _load_taxonomy()
# reports drift on import rather than letting the two definitions rot apart silently.

# What each class is ALLOWED to do to the board. THIS TABLE, NOT THE MODEL, is what stops a P&L
# update from minting a contract: a class outside CONTRACT_CLASSES cannot reach a card no matter
# how many fields the model filled in or how confident it says it is.
CONTRACT_CLASSES = {"ENTRY", "WATCH"}                    # the only classes that MINT a new record
MGMT_CLASSES = {"ADD", "TRIM", "EXIT", "STOP_SET", "STOP_MOVE", "TARGET_SET", "PNL_UPDATE"}
DROP_CLASSES = {"RECAP", "COMMENTARY", "WARNING", "QUESTION", "CHAT", "NOISE",
                "EDIT", "REPOST", "NON_PROVIDER"}
# Per taxonomy §2.1: carding anything from one of these is CRITICAL — it mints a position out of
# nothing. That is failure F2 (the SMCI 32.16C).
NEVER_CARD = {"PNL_UPDATE", "RECAP", "COMMENTARY", "NON_PROVIDER", "NOISE", "CHAT", "QUESTION",
              "WARNING"}

# Desk routes, i.e. what the deterministic layer actually did with the message.
ROUTE_CARD, ROUTE_FILL, ROUTE_MGMT = "CARD", "FILL", "MGMT"
ROUTE_DROP, ROUTE_ASK = "REJECT", "ASK"
CARD_ROUTES = {ROUTE_CARD, ROUTE_FILL}     # the routes that put or move something on the board

_CLASS_ROUTE = {c: ROUTE_CARD for c in CONTRACT_CLASSES}
_CLASS_ROUTE["FILL"] = ROUTE_FILL          # upgrades an existing watch; degrades to ENTRY if none
_CLASS_ROUTE.update({c: ROUTE_MGMT for c in MGMT_CLASSES})
_CLASS_ROUTE.update({c: ROUTE_DROP for c in DROP_CLASSES})
_CLASS_ROUTE["MUST_ASK"] = ROUTE_ASK

TAXONOMY_FILE = Path(__file__).parent / "eval" / "message_taxonomy.md"
_taxonomy_note = "interim — eval/message_taxonomy.md not present"


def _load_taxonomy():
    """Reconcile with the parallel agent's spec when it lands. We do NOT write that file; we read
    it and report drift, because a taxonomy that quietly disagrees with the corpus it is scored
    against is worse than one that is obviously stale."""
    global _taxonomy_note
    try:
        txt = TAXONOMY_FILE.read_text()
    except Exception:
        return {"ok": False, "note": _taxonomy_note}
    # Read the AUTHORITATIVE type table (§2.1) only. An earlier version scanned the whole document
    # for backticked ALLCAPS and "found" 12 classes that are really tickers and card statuses
    # (PLTR, WATCHING, ODTE, 0DTE...), reporting drift that did not exist. The table is the spec.
    body = re.split(r"^###?\s*\d", txt[txt.find("### 2.1"):], maxsplit=2, flags=re.M)
    body = body[1] if len(body) > 1 else txt
    found = set()
    for line in body.splitlines():
        m = re.match(r"\s*\|\s*`([A-Z][A-Z_]{2,20})`(?:\s*/\s*`([A-Z][A-Z_]{2,20})`)?\s*\|", line)
        if m:
            found.update(g for g in m.groups() if g)
    known = set(CLASSES)
    missing, extra = sorted(known - found), sorted(found - known)
    _taxonomy_note = ("in sync" if not missing and not extra else
                      f"DRIFT: router-only={missing} spec-only={extra}")
    return {"ok": True, "note": _taxonomy_note, "spec_classes": sorted(found),
            "router_classes": CLASSES, "missing": missing, "extra": extra}


# ---------------------------------------------------------------- L0: deterministic prefilter
# The LLM must not be asked about a message whose class is not in doubt. This is a cost lever, but
# more importantly it is a BLAST-RADIUS lever: a message the prefilter owns is a message a model
# mistake cannot touch. Every rule here is written to be high-PRECISION, not high-recall — when a
# rule is not certain it returns None and the message goes to the model.

_URL_ONLY = re.compile(r"^\s*(?:<)?https?://\S+(?:>)?\s*$", re.I)
_MENTION_ONLY = re.compile(r"^\s*(?:@\w+|<@!?\d+>|[\W_])+\s*$")
_PCT = re.compile(r"\d{1,4}\s*%")
# A RISK percent is not a P&L percent. Option King writes "SPY 750C at 0.80 0DTE SL 30%" on every
# single entry; treating that 30% as "this message is about an existing position" would send every
# one of his entries to the model for no reason (and, worse, make R2 look like it fires on entries).
_RISK_PCT = re.compile(r"(?:\bs\s*/?\s*l\b|\bstop(?:\s*loss)?\b|\brisk\b|\btrail\b)[^%]{0,12}?\d{1,3}\s*%"
                       r"|\d{1,3}\s*%\s*(?:stop|sl|risk|trail)", re.I)
# Discord decoration that carries no trading content and sits in front of ~40% of real entries.
_DECOR = re.compile(r"@everyone|@here|<@!?\d+>|@[A-Za-z][\w' ]{0,28}(?:Alerts|Trade[s]?|Team|Room)\b", re.I)
_EXIT_VERB = re.compile(r"\b(out|sold|selling|trim(?:med|ming)?|scal(?:ed|ing)|de-?risk|"
                        r"cut(?:ting)?|closed|closing|stopped out|took profit|secure)\b", re.I)
_STOP_VERB = re.compile(r"\b(?:s/?l|stop)\b.{0,20}\b(?:to|at|@|be|b/e|breakeven|break even|entry)\b", re.I)
_PNL_WORD = re.compile(r"\b(?:up|down|green|red|running|printing|profit|gain|loss)\b", re.I)
# a full, unambiguous contract on one line: TICKER STRIKE(C|P) [expiry] @price
_CLEAN_CONTRACT = re.compile(
    r"^\W*\$?([A-Za-z]{1,5})\s+\$?(\d{1,5}(?:\.\d{1,2})?)\s*(c|p|calls?|puts?)\b(?P<rest>.*)$",
    re.I | re.S)
_AT_PRICE = re.compile(r"(?:@|\bat\s*)\$?(\d{1,3}(?:\.\d{1,2})?)\b", re.I)

PREFILTER_RULES = []          # (name, fn) — populated below, kept for the eval report


def prefilter(text, provider=None, regex_sig=None, carded=False):
    """L0. Return {"cls", "why", "confidence": 1.0} when the class is not in doubt, else None.

    Returning None is always safe (it just costs one Haiku call). Returning a WRONG class here is
    NOT safe, so every rule below has to be defensible on its own."""
    t = (text or "").strip()
    if not t:
        return {"cls": "NOISE", "why": "empty"}
    low = t.lower()

    # R1 — a bare link, a bare mention, an emoji, a stray token. No trading content is expressible.
    #      The length test requires NO DIGITS: mike posts bare prices like ".5" and ".12" as fills,
    #      and the corpus labels those MUST_ASK, not NOISE. A short string with a number in it is
    #      never safely ignorable on this desk.
    if _URL_ONLY.match(t) or _MENTION_ONLY.match(t) or (len(t) < 4 and not re.search(r"\d", t)):
        return {"cls": "NOISE", "why": "no_content"}

    # A Discord edit footer. server.ingest handles edits structurally (msg_id -> drop the stale
    # card, re-card the corrected text), so the prefilter must not answer for them: an edited
    # entry still LOOKS like a clean entry and R3 would happily card it a second time.
    edited = bool(re.search(r"\(edited\)", t, re.I))

    # A stop-loss percent is risk sizing, not performance. Strip it before asking "is this a
    # message about how an existing position is doing?".
    pnl_pct = bool(_PCT.search(_RISK_PCT.sub(" ", t)))

    # R2 — a percentage plus P&L language and NO fresh contract syntax. This is the SMCI shape.
    #      "303% on $SMCI calls" names a ticker and the word "calls" but no strike adjacent to it;
    #      the only numbers are a return and a price target. Nothing here opens a position.
    #      Deliberately does NOT fire when an exit verb is present — that is a TRIM/EXIT, and
    #      telling those apart is worth a model call because the desk acts on them differently.
    if pnl_pct and _PNL_WORD.search(low) and not _EXIT_VERB.search(low):
        if not _has_fresh_contract_syntax(t):
            return {"cls": "PNL_UPDATE", "why": "percent_pnl_no_contract"}

    # R3 — a clean, complete contract with a price and no exit/P&L language. This is the
    #      overwhelming majority of real entries ("QQQ 708C @ 1.83") and the regex owns it
    #      outright. Requiring the STRIKE to sit adjacent to the c/p token is what keeps a price
    #      target ("target 32.16") from qualifying, and cross-checking against the regex parse
    #      means the prefilter and the parser have to agree before the model is skipped.
    if regex_sig and regex_sig.get("strike") and regex_sig.get("premium") is not None \
            and not pnl_pct and not edited \
            and not _EXIT_VERB.search(low) and not _STOP_VERB.search(low):
        core = _DECOR.sub(" ", t).strip()
        m = _CLEAN_CONTRACT.match(core)
        if m and _AT_PRICE.search(m.group("rest") or ""):
            tk = (regex_sig.get("ticker") or "").upper()
            if tk and m.group(1).upper() == tk and \
                    abs(float(m.group(2)) - float(regex_sig["strike"])) < 1e-6:
                watchy = bool(re.search(r"\b(watch(?:ing)?|eyeing|looks nice|if it|considering)\b", low))
                return {"cls": "WATCH" if watchy else "ENTRY", "why": "clean_contract_line"}

    # R4 — a RECAP. Several contracts, each with a closed result. Taxonomy §8 rates carding out of
    #      a recap as CRITICAL: every contract in it is a trade that ALREADY ENDED, so any card
    #      minted here is a position that was never opened. The signature is unmistakable and
    #      cheap: three or more strike-bound contracts plus multiple signed percentages.
    if len(re.findall(r"(?<![\d.])\d{1,5}(?:\.\d{1,2})?\s*(?:c|p|calls?|puts?)\b", t, re.I)) >= 3 \
            and len(_PCT.findall(t)) >= 2:
        if re.search(r"\brecap\b|\bsummary\b|→|->", low) or len(_PCT.findall(t)) >= 3:
            return {"cls": "RECAP", "why": "multi_contract_recap"}
    return None


def _has_fresh_contract_syntax(t):
    """Is there a STRIKE directly bound to a call/put token? '134 calls' yes; 'SMCI calls' no;
    'price target 32.16' no. This is the single test that separates a new contract from a
    number that happens to be in a sentence about one."""
    return bool(re.search(r"(?<![\d.])\d{1,5}(?:\.\d{1,2})?\s*(?:c|p|calls?|puts?)\b", t, re.I)
                or re.search(r"\b(?:calls?|puts?)\s*(?:@\s*)?(?<![\d.])\d{2,5}\b", t, re.I))


# ---------------------------------------------------------------- the conversational Book
# The DURABLE, structured half of provider context. llm_parse._Context already keeps the raw
# message ring (that is the model's reading material); this keeps the desk's own answer to
# "what does this provider currently have on?", which is what a bare "added 1.05" must resolve
# against. It is deliberately a separate concern: the ring is evidence, the Book is state.

WATCH_TTL = 86400          # matches server._apply_entry_fill's 24h watch->fill window exactly
OPEN_TTL = 30 * 86400
CLOSED_TTL = 7 * 86400
PER_PROVIDER = 60


def _key(tk, strike, right):
    try:
        s = f"{float(strike):g}"
    except (TypeError, ValueError):
        s = str(strike)
    return f"{str(tk).upper()}|{s}|{str(right).upper()}"


class Book:
    """Per-provider position book: what is being WATCHED, what is OPEN, what recently CLOSED.

    SURVIVES RESTART TWO WAYS, on purpose:
      1. write-through to channels/_route_context.json (safe_io: atomic + backed up);
      2. sync_from_signals() rebuilds the whole thing from server.SIGNALS, which is the desk's
         authoritative state and is itself persisted.
    (2) is the real guarantee. (1) exists so the book is warm the instant the process comes up and
    so entries the LLM learned about (a fill the regex never carded) are not lost. Today's context
    is memory-first and degrades to NOTHING on restart; this degrades to the desk's own truth."""

    def __init__(self, path=_CTX_FILE):
        self.path = Path(path)
        self._d = defaultdict(dict)          # provider -> key -> row
        self._dirty = 0
        self.load()

    # ---- mutation
    def put(self, provider, ticker, strike, right, state, expiry=None, premium=None,
            card_id=None, ts=None, src="card"):
        if not provider or not ticker or strike is None or not right:
            return None
        k = _key(ticker, strike, right)
        row = self._d[provider].get(k) or {}
        row.update({"key": k, "ticker": str(ticker).upper(), "strike": _f(strike),
                    "right": str(right).upper(), "state": state, "ts": ts or time.time()})
        if expiry:
            row["expiry"] = expiry
        if premium is not None:
            row["premium"] = _f(premium)
        if card_id:
            row["card_id"] = card_id
        row["src"] = src
        self._d[provider][k] = row
        self._trim(provider)
        self._dirty += 1
        if self._dirty >= 5:
            self.save()
        return row

    def close(self, provider, ticker, strike, right):
        k = _key(ticker, strike, right)
        row = self._d.get(provider, {}).get(k)
        if row:
            row["state"] = "CLOSED"
            row["ts"] = time.time()
            self._dirty += 1
        return row

    def _trim(self, provider):
        rows = self._d[provider]
        now = time.time()
        for k, r in list(rows.items()):
            ttl = {"WATCHING": WATCH_TTL, "OPEN": OPEN_TTL}.get(r.get("state"), CLOSED_TTL)
            if now - r.get("ts", 0) > ttl:
                rows.pop(k, None)
        if len(rows) > PER_PROVIDER:
            for k, _ in sorted(rows.items(), key=lambda kv: kv[1].get("ts", 0))[:len(rows) - PER_PROVIDER]:
                rows.pop(k, None)

    # ---- the authoritative rebuild
    def sync_from_signals(self, signals):
        """Rebuild from the desk's own card list. Cheap, idempotent, and the reason a lost context
        file is an inconvenience rather than a data loss."""
        if not signals:
            return 0
        n = 0
        for s in signals[-400:]:
            sig = s.get("sig") or {}
            if not sig.get("ticker") or sig.get("strike") is None or not sig.get("type"):
                continue
            st = s.get("status")
            state = ("WATCHING" if st == "WATCHING" else
                     "OPEN" if st in ("NEW", "TAKEN") else "CLOSED")
            try:
                ts = time.mktime(time.strptime((s.get("iso") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = time.time()
            self.put(s.get("provider"), sig["ticker"], sig["strike"], sig["type"], state,
                     expiry=sig.get("expiry"), premium=sig.get("premium"),
                     card_id=s.get("id"), ts=ts, src="sync")
            n += 1
        self.save()
        return n

    # ---- query
    def rows(self, provider, states=("WATCHING", "OPEN")):
        now = time.time()
        out = []
        for r in (self._d.get(provider) or {}).values():
            if r.get("state") not in states:
                continue
            ttl = {"WATCHING": WATCH_TTL, "OPEN": OPEN_TTL}.get(r.get("state"), CLOSED_TTL)
            if now - r.get("ts", 0) <= ttl:
                out.append(r)
        return sorted(out, key=lambda r: r.get("ts", 0), reverse=True)

    def resolve(self, provider, text, cls, hint_ticker=None):
        """WHICH position is this message about?

        THIS FUNCTION IS DELIBERATELY NOT ALLOWED TO GUESS. A provider runs several positions at
        once; picking the most recent one, or the one whose premium is nearest the number in the
        message, attaches a fill or an exit to possibly the WRONG trade — and unlike a missed
        message, nothing in the log tells you it happened. So:
            exactly one candidate survives the filters -> resolved
            more than one                              -> ambiguous  (the desk ASKS)
            none                                       -> none       (fall through unchanged)

        Returns {"status", "contract", "candidates", "how"}."""
        states = (("WATCHING", "OPEN") if cls in ("FILL", "ADD", "ENTRY")
                  else ("OPEN",) if cls in MGMT_CLASSES
                  else ("WATCHING", "OPEN"))
        cand = self.rows(provider, states)
        if not cand:
            return {"status": "none", "contract": None, "candidates": [], "how": "book_empty"}
        how = []

        # Vocabulary = the desk's known-symbol list PLUS the symbols this provider actually has on
        # the book. manage.KNOWN holds 31 symbols; the corpus contains 14 more (BABA, NBIS, HIMS,
        # HOOD, CELH, WMT...), so a KNOWN-only match silently failed to narrow on those and left
        # every candidate standing. Symbols already on the book are verified — they came off real
        # carded contracts — so widening the vocabulary this way cannot invent a ticker.
        tks = _tickers_in(text, extra={r["ticker"] for r in cand})
        if tks:
            narrowed = [r for r in cand if r["ticker"] in tks]
            if narrowed:
                cand, _ = narrowed, how.append("ticker")
            else:
                # the message names a ticker this provider has nothing open in -> not a referent
                return {"status": "none", "contract": None, "candidates": [],
                        "how": "named_ticker_not_in_book"}

        # A ticker the MODEL read as this message's subject. It is a REPORTED value, grounded in a
        # verbatim quote — the same standing as any other field it reports — so it may narrow, but
        # it may not select: if the hint matches nothing we ignore it rather than trusting it over
        # the book. This is what disambiguates "GOOGL pulling back ... better than AAPL / IN AT
        # 0.93" when the provider is watching BOTH names.
        if hint_ticker and len(cand) > 1:
            narrowed = [r for r in cand if r["ticker"] == str(hint_ticker).upper()]
            if narrowed:
                cand, _ = narrowed, how.append("subject")

        strikes = _strikes_in(text)
        if strikes:
            narrowed = [r for r in cand if any(abs(r["strike"] - s) < 1e-6 for s in strikes)]
            if narrowed:
                cand, _ = narrowed, how.append("strike")
            else:
                # THE MESSAGE NAMES A STRIKE AND NOTHING ON THE BOOK HAS IT. That is positive
                # evidence this is a DIFFERENT contract, not a referent to an existing one.
                # Falling through to the ticker match here would have linked mike's
                # "META 675 2.5" to his open META 665P — a wrong-position link, which the
                # taxonomy rates high-severity because it corrupts two positions at once.
                return {"status": "none", "contract": None, "candidates": [],
                        "how": "stated_strike_not_in_book"}

        # A FILL fills something you were WATCHING. When both watches and open positions are
        # candidates, the watches are the referent class — this is contract semantics, not a
        # tie-break heuristic, so it is allowed where "pick the most recent" is not.
        if cls == "FILL" and len(cand) > 1:
            watching = [r for r in cand if r.get("state") == "WATCHING"]
            if watching:
                cand, _ = watching, how.append("watching")

        if len(cand) == 1:
            return {"status": "resolved", "contract": cand[0], "candidates": cand,
                    "how": "+".join(how) or "sole_candidate"}
        return {"status": "ambiguous", "contract": None, "candidates": cand[:6],
                "how": "+".join(how) or "multiple_open"}

    # ---- persistence
    def save(self):
        try:
            import safe_io
            payload = {p: list(rows.values()) for p, rows in self._d.items() if rows}
            safe_io.safe_write_json(self.path, payload, min_keep_ratio=0.0)   # regenerable cache
            self._dirty = 0
        except Exception:
            pass

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            return
        now = time.time()
        for prov, rows in (data or {}).items():
            for r in rows or []:
                ttl = {"WATCHING": WATCH_TTL, "OPEN": OPEN_TTL}.get(r.get("state"), CLOSED_TTL)
                if now - r.get("ts", 0) <= ttl and r.get("key"):
                    self._d[prov][r["key"]] = r

    def stats(self):
        return {p: {"watching": sum(1 for r in rows.values() if r.get("state") == "WATCHING"),
                    "open": sum(1 for r in rows.values() if r.get("state") == "OPEN"),
                    "closed": sum(1 for r in rows.values() if r.get("state") == "CLOSED")}
                for p, rows in self._d.items() if rows}


BOOK = Book()


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


_TICKER_RE = re.compile(r"\$([A-Za-z]{1,5})\b|\b([A-Z]{2,5})\b")


def _tickers_in(text, extra=None):
    """Tickers explicitly named. Matched against a CLOSED vocabulary (manage.KNOWN plus whatever
    the caller supplies from the book) so this can never invent a symbol out of a stray word —
    every provider message is full of 2-5 letter uppercase tokens that are not tickers."""
    try:
        import manage as M
        known = set(M.KNOWN)
    except Exception:
        known = set()
    known |= {str(t).upper() for t in (extra or ())}
    out = set()
    for m in _TICKER_RE.finditer(text or ""):
        tok = (m.group(1) or m.group(2) or "").upper()
        if tok in known:
            out.add(tok)
    return out


def _strikes_in(text):
    return {float(m.group(1)) for m in
            re.finditer(r"(?<![\d.])(\d{1,5}(?:\.\d{1,2})?)\s*(?:c|p|calls?|puts?)\b", text or "", re.I)}


# ---------------------------------------------------------------- L2: the model
# One call does BOTH jobs — classify AND extract — because they are the same read of the same
# text and splitting them would double the cost and let the two halves disagree.

def _nullable(t, **kw):
    return {"anyOf": [dict({"type": t}, **kw), {"type": "null"}]}


ROUTE_TOOL = {
    "name": "record_route",
    "description": ("Classify the LAST message from a copy-trading options provider and record "
                    "only what that message itself says."),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "message_class": {"type": "string", "enum": CLASSES},
            "names_new_contract": {"type": "boolean"},
            "refers_to_earlier_position": {"type": "boolean"},
            "ambiguous_referent": {"type": "boolean"},
            "ticker": _nullable("string"),
            "strike": _nullable("number"),
            "right": {"anyOf": [{"type": "string", "enum": ["C", "P"]}, {"type": "null"}]},
            "expiry": _nullable("string"),
            "premium": _nullable("number"),
            "confidence": {"type": "number"},
            "unresolved": {"type": "array", "items": {"type": "string"}},
            "quotes": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "message_class": _nullable("string"),
                    "ticker": _nullable("string"),
                    "strike": _nullable("string"),
                    "expiry": _nullable("string"),
                    "premium": _nullable("string"),
                },
                "required": ["message_class", "ticker", "strike", "expiry", "premium"],
            },
        },
        "required": ["message_class", "names_new_contract", "refers_to_earlier_position",
                     "ambiguous_referent", "ticker", "strike", "right", "expiry", "premium",
                     "confidence", "unresolved", "quotes"],
    },
}

SYSTEM = """You route messages from a copy-trading options Discord into exactly one class. A live \
trading desk acts on your answer, so the classes are defined by what the desk must DO, not by tone.

CLASSES
  ENTRY        In, or entering right now, AND this message fully specifies the contract.
  WATCH        A named setup they are NOT in: eyeing, resting a limit, "looks nice", "may enter".
  FILL         A setup watched EARLIER is now filled ("in @3.40", "got in at 1.05"). This message
               does NOT need to name the contract.
  ADD          Scale-in / average-down on a position that already exists.
  TRIM         Selling PART of an existing position.
  EXIT         Closing the whole position — target, stop, or voluntary.
  STOP_SET     Setting a stop on an open position.
  STOP_MOVE    Moving an existing stop (to breakeven, to a level).
  TARGET_SET   Stating or moving a profit target.
  PNL_UPDATE   Reporting % or $ on an EXISTING position ("up 303%", "green", "11%"). Includes
               brags. NEVER mints a contract. This is the single most misread class: a message can
               name a ticker, say "calls", and quote several numbers while opening nothing.
  RECAP        End-of-day or weekly summary listing several trades that ALREADY CLOSED.
  COMMENTARY   Market view, level talk, chart narration, plan-for-the-day, no actionable contract.
  WARNING      Risk caution: "manage ur risk", "go small", "it is risky", "you're on your own".
  QUESTION     A question to the room.
  CHAT         Banter, off-topic.
  NOISE        URL-only, GIF, emoji, empty, test message.
  EDIT         A Discord edit of a message already posted (carries an "(edited)" footer).
  REPOST       The scraper replayed a message already sent (identical text, same provider).
  NON_PROVIDER Not from a trading provider — a bot or ops feed.
  MUST_ASK     An action is clearly intended, but the referent or a required field cannot be
               recovered from the text or the context you were given.

RULES
1. Classify the LAST message only. Earlier messages exist ONLY to tell you whether something was
   already set up or already open.
2. NUMBER ROLES. Every number in these messages already means something. A number is a STRIKE only
   when it is bound to "call"/"put"/"c"/"p" ("134 calls", "$73c", "197.5c"). A price target
   ("my price target for today is 32.16"), a stop ("s/l 1.29"), a chart level, a quantity and a
   return percentage are NOT strikes and NOT premiums. Never reuse a number that already has a
   role, and never invent one that is missing.
3. `names_new_contract` is true ONLY when THIS message states a contract to OPEN. A FILL of an
   earlier setup is false. Anything managing an open position is false. RECAP is false — every
   contract in a recap is a trade that already ended.
4. Fill ticker/strike/right/expiry/premium ONLY from what THIS message states. Do NOT copy a
   contract out of an earlier message: deterministic code does that linking, and it does it more
   safely than you can. Leave the fields null and set refers_to_earlier_position=true instead.
5. `quotes` must hold the VERBATIM substring you read each value from, copied exactly from the
   text. Anything you cannot point at is null. quotes.message_class is the phrase that decides
   the class.
6. `ambiguous_referent` is true when the message acts on a position it does not name and more than
   one position could be meant. When that is true the class is MUST_ASK. Choosing one is worse
   than asking: it attaches a fill or an exit to the wrong trade, and nothing in the log shows it.
7. `confidence` is 0-1 for the CLASS. Be harsh. A wrong class either puts a contract that does not
   exist in front of a trader, or loses a real fill.

EXAMPLES (all real messages from this desk)
  "303% on $SMCI calls. Best overnight trade this month. My price target for today is $32.16"
      -> PNL_UPDATE, names_new_contract false, strike null.
         32.16 is an underlying target and is not even a listed SMCI strike. The desk carded this
         as an "SMCI 32.16C". That is the failure this class exists to prevent.
  [earlier] "Watching $PLTR $134 calls, entry around 2.05"  [last] "Getting in PLTR here @2.03"
      -> FILL, names_new_contract false, refers_to_earlier_position true, premium 2.03,
         ticker PLTR, strike null.
  [earlier] "Watching GOOGL 370C"   [last] "in @3.40 july 31 expiry"
      -> FILL, refers_to_earlier_position true, premium 3.40, expiry "july 31", strike null.
         "july 31" is a date. It is not strike 31.
  [earlier names TWO open trades]   [last] "added 1.05"
      -> MUST_ASK, ambiguous_referent true.
  "Recap 6 July 2026 / META 600C @ 0.30 +400% / SPY 750C @ 0.58 +180% / IWM 300C @ 0.62 +50%"
      -> RECAP, names_new_contract false, all fields null.
  "QQQ 708C @ 1.83"                          -> ENTRY, names_new_contract true, fields filled.
  "out half here 2.40, moving stop to break even"  -> TRIM.
  "11%"                                       -> PNL_UPDATE, all fields null.
  "manage ur risk guys"                       -> WARNING."""


def _render(window, text):
    lines = []
    for m in window or []:
        age = max(0, int((time.time() - m.get("ts", time.time())) / 60))
        lines.append(f"[earlier, {age} min ago]\n{m['text']}")
    lines.append(f"[last message — classify THIS one]\n{text}")
    return "\n\n".join(lines)


_GROUND_FIELDS = {"ticker": "ticker", "strike": "strike", "expiry": "expiry", "premium": "premium"}


def classify(text, provider=None, window=None):
    """One routing call. Returns a grounded route dict, or None on ANY failure (fail open).
    Blocking — worker thread only."""
    if not LP:
        return None
    if window is None:
        window = LP.CONTEXT.window(provider, exclude_text=text)
    prompt = _render(window, text)
    raw = LP.call_tool(SYSTEM, ROUTE_TOOL, prompt, model=MODEL, max_tokens=MAX_TOKENS)
    if raw is None:
        return None

    # --- grounding. Same non-model substring test llm_parse uses, applied to the routing fields.
    dropped = LP.ground(raw, prompt, _GROUND_FIELDS)
    for fld in ("strike", "premium"):
        if raw.get(fld) is not None and not LP.numeric_in_text(raw[fld], prompt):
            raw[fld] = None
            dropped.append(fld + ":not_in_text")
    # The CLASS itself is grounded too: a class the model cannot point at in the text is a class
    # it inferred rather than read. That does not veto the class (some classes are genuinely
    # inferred from absence), but it caps confidence, which is what gates a promotion.
    cq = LP._norm((raw.get("quotes") or {}).get("message_class"))
    class_grounded = bool(cq and cq in LP._norm(prompt))
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except Exception:
        conf = 0.0
    if dropped:
        conf = min(conf, 0.5)
    if not class_grounded:
        conf = min(conf, 0.7)
    if raw.get("message_class") not in CLASSES:
        return None                                   # strict schema should prevent this; belt+braces
    raw.update(confidence=conf, class_grounded=class_grounded, ungrounded_dropped=dropped,
               _prompt_len=len(prompt), _ctx_msgs=len(window or []))
    return raw


# ---------------------------------------------------------------- adjudication
# Pure function: route dict + what the desk did -> a VERDICT. No network, no desk state mutation,
# so it is directly unit-testable and every safety rule is visible in one place.

def adjudicate(route, desk_route, book_verdict=None, carded_status=None):
    """desk_route: what the deterministic layer did (CARD / MGMT / REJECT / ASK).
    book_verdict: Book.resolve() output, or None.
    carded_status: the status of the card the desk produced, if any.

    action:
      KEEP     — the model agrees, or disagrees in a way not worth acting on.
      DEMOTE   — the desk carded something that is really an update / noise. Retract it.
                 CHEAP: allowed on the model's judgement alone.
      PROMOTE  — the desk dropped something that is really an entry or a fill. EXPENSIVE: this is
                 only a REQUEST; the caller must re-enter it through ingest() so every
                 deterministic guard runs.
      ASK      — actionable but the referent is ambiguous. Never guess.
    """
    if not route:
        return {"action": "KEEP", "reason": "no route"}
    cls = route.get("message_class")
    conf = float(route.get("confidence") or 0)
    want = _CLASS_ROUTE.get(cls, ROUTE_DROP)
    base = {"cls": cls, "confidence": conf, "want": want, "desk": desk_route,
            "quotes": route.get("quotes"), "ungrounded": route.get("ungrounded_dropped")}

    if route.get("ambiguous_referent") or cls == "MUST_ASK":
        # The model says the referent is unrecoverable FROM THE TEXT. It has not seen the book, so
        # that is a statement about the sentence, not about the desk. If the book resolves to
        # exactly one position, the referent IS recoverable — attach it so the operator gets a
        # one-tap confirmation instead of an open question.
        #
        # Note what this does NOT do: it does not promote. Deterministic evidence is allowed to
        # make a question more specific; it is not allowed to turn the model's own "I don't know"
        # into a live card. The action stays ASK.
        bv = book_verdict or {}
        if bv.get("status") == "resolved":
            c = bv["contract"]
            return {**base, "action": "ASK", "resolved_via": bv.get("how"),
                    "sig": {"ticker": c["ticker"], "strike": c["strike"], "type": c["right"],
                            "expiry": c.get("expiry"), "premium": route.get("premium")},
                    "candidates": [c],
                    "reason": f"referent unclear in the text, but only one position fits "
                              f"({c['ticker']} {c['strike']:g}{c['right']}) — confirm"}
        return {**base, "action": "ASK", "candidates": bv.get("candidates"),
                "reason": "ambiguous referent — more than one position fits"}

    card_routes = CARD_ROUTES

    # ---- DEMOTION. The board gets quieter. The model may do this on its own judgement.
    if desk_route in card_routes and want not in card_routes:
        if carded_status in ("TAKEN", "CLOSED"):
            # Real money is already committed. A model opinion does not get to rewrite history;
            # it gets to annotate it.
            return {**base, "action": "KEEP", "reason": f"card already {carded_status} — annotate only",
                    "annotate": True}
        if conf < CONF_DEMOTE:
            return {**base, "action": "KEEP", "reason": f"demotion below confidence floor ({conf:.2f})"}
        return {**base, "action": "DEMOTE",
                "reason": f"desk carded this; it reads as {cls}"
                          + (" (no new contract named)" if not route.get("names_new_contract") else "")}

    # ---- CARD <-> FILL disagreement. Both touch the board, so neither is a demotion, and
    #      re-linking a card to a different position is exactly the "wrong position" error the
    #      taxonomy rates high-severity. Surface it, do not act on it.
    if desk_route in card_routes and want in card_routes and desk_route != want:
        return {**base, "action": "KEEP", "annotate": True,
                "reason": f"desk routed {desk_route}, this reads as {cls} — relink is a human call"}

    # ---- PROMOTION. The board gets louder. Everything below is a REQUEST, not a decision.
    if desk_route in (ROUTE_DROP, ROUTE_MGMT) and want in card_routes:
        # HARD GUARD, checked before anything else: a class that can never open a position can
        # never reach a card, whatever the model filled in. Unreachable via _CLASS_ROUTE today;
        # kept because _CLASS_ROUTE is a table someone will edit.
        if cls in NEVER_CARD:
            return {**base, "action": "KEEP", "reason": f"{cls} may never mint a contract"}
        if conf < CONF_PROMOTE:
            return {**base, "action": "ASK",
                    "reason": f"looks like {cls} but confidence {conf:.2f} — confirm by hand"}
        # Where does the contract come from?
        #   (a) this message states it -> use the GROUNDED fields;
        #   (b) it is a fill of something earlier -> use the BOOK, never the model.
        if route.get("names_new_contract") and route.get("ticker") and route.get("strike") is not None:
            sig = {"ticker": route["ticker"], "strike": route["strike"],
                   "type": route.get("right") or "C",
                   "expiry": LP._strip_year(route.get("expiry")) if LP else route.get("expiry"),
                   "premium": route.get("premium"), "watch": cls == "WATCH"}
            return {**base, "action": "PROMOTE", "source": "message", "sig": sig,
                    "reason": f"{cls} the desk dropped; contract stated in the message"}
        bv = book_verdict or {}
        if bv.get("status") == "resolved":
            c = bv["contract"]
            sig = {"ticker": c["ticker"], "strike": c["strike"], "type": c["right"],
                   "expiry": (LP._strip_year(route.get("expiry")) if LP else route.get("expiry"))
                             or c.get("expiry"),
                   "premium": route.get("premium"), "watch": cls == "WATCH"}
            return {**base, "action": "PROMOTE", "source": "book", "sig": sig,
                    "resolved_via": bv.get("how"), "candidates": 1,
                    "reason": f"{cls} of a position this provider set up earlier "
                              f"({c['ticker']} {c['strike']}{c['right']}, matched on {bv.get('how')})"}
        if bv.get("status") == "ambiguous":
            return {**base, "action": "ASK", "candidates": bv.get("candidates"),
                    "reason": f"{cls}, but {len(bv.get('candidates') or [])} open positions could be meant"}
        return {**base, "action": "ASK",
                "reason": f"{cls} with no contract stated and nothing in the book to attach it to"}

    # ---- the desk dropped it and the model agrees there is management here. Cheap, informative,
    #      but it does not create a card, so it is applied as an annotation only.
    if desk_route == ROUTE_DROP and want == ROUTE_MGMT and conf >= CONF_DEMOTE:
        return {**base, "action": "KEEP", "annotate": True,
                "reason": f"reads as {cls} (management) — desk dropped it"}
    return {**base, "action": "KEEP", "reason": "desk route stands"}


# ---------------------------------------------------------------- the desk adapter
# server.py fills this in ONE clearly-marked block. Unbound -> every effect is a no-op, which is
# what makes this module safe to import from anywhere including the evaluator.

DESK = {"signals": None, "retract": None, "promote": None, "alert": None, "save": None}
_bound = False


def bind(**kw):
    """INTEGRATION POINT: server.py hands the router the four things it is allowed to do."""
    global _bound
    DESK.update({k: v for k, v in kw.items() if k in DESK})
    _bound = bool(DESK.get("promote") or DESK.get("retract"))
    try:
        if DESK.get("signals"):
            BOOK.sync_from_signals(DESK["signals"]())
    except Exception:
        pass
    return _bound


# ---------------------------------------------------------------- async plumbing
# Identical discipline to llm_parse: the worker does NETWORK ONLY; every mutation of desk state
# happens back on the main thread in drain(). No locks, no concurrent mutation of SIGNALS.

_JOBS = queue.Queue(maxsize=QUEUE_MAX)
_RESULTS = queue.Queue()
_worker = None
STATS = defaultdict(int)
RECENT = deque(maxlen=60)          # last N routing decisions, for the dashboard / debugging

# OWN dedup set, deliberately NOT llm_parse's.
# Measured bug (e2e, 2026-07-22): sharing llm_parse's `_seen` starved this module completely.
# _llm_review() runs INSIDE ingest and submits first, so by the time the router's hook ran, every
# message it cared about was already marked "seen" and skipped — router stats read
# {'messages': 6, 'dedup': 3, 'queued': 3} and the PLTR fill was never routed. The two layers ask
# different questions about the same text and must each get to ask. The overlap is bounded: the
# ~9.3% of messages llm_parse triggers on are the only ones billed twice (~4 calls/day, ~$0.015).
_SEEN = {}
_SEEN_TTL = 3600


def available():
    return bool(LP) and LP.available()


def note(provider, text, ts=None, msg_id=None):
    """INTEGRATION POINT: record every message for conversational context. Delegates to
    llm_parse's ring so there is exactly ONE raw-message window on the desk."""
    if LP:
        try:
            LP.note_message(provider, text, ts, msg_id)
        except Exception:
            pass


def observe(entry):
    """INTEGRATION POINT: a card was created / changed state. Keep the Book current."""
    try:
        sig = (entry or {}).get("sig") or {}
        if not sig.get("ticker") or sig.get("strike") is None or not sig.get("type"):
            return
        st = entry.get("status")
        state = "WATCHING" if st == "WATCHING" else "OPEN" if st in ("NEW", "TAKEN") else "CLOSED"
        BOOK.put(entry.get("provider"), sig["ticker"], sig["strike"], sig["type"], state,
                 expiry=sig.get("expiry"), premium=sig.get("premium"), card_id=entry.get("id"))
    except Exception:
        pass


def _run():
    while True:
        job = _JOBS.get()
        try:
            route = classify(job["text"], provider=job.get("provider"), window=job.get("window"))
            _RESULTS.put({**job, "route": route, "done_ts": time.time()})
        except Exception as e:                             # a worker must never die
            _RESULTS.put({**job, "route": None, "error": str(e)[:200]})
        finally:
            _JOBS.task_done()


def on_message(text, provider, desk_route, card_id=None, carded_status=None, regex_sig=None,
               channel=None):
    """INTEGRATION POINT — call once per message, AFTER the deterministic layer has decided.

    Returns a small dict describing what L0 did, and NEVER blocks: the only work on this path is
    a regex prefilter and a queue put. Measured: see `python3 llm_eval.py route-latency`."""
    global _worker
    STATS["messages"] += 1
    if not text or not provider:
        return {"skipped": "no text/provider"}

    pre = prefilter(text, provider=provider, regex_sig=regex_sig, carded=(desk_route == ROUTE_CARD))
    if pre:
        STATS["prefiltered"] += 1
        STATS["pre_" + pre["why"]] += 1
        want = _CLASS_ROUTE.get(pre["cls"], ROUTE_DROP)
        # The prefilter is allowed to DISAGREE, but only in the cheap direction. A prefilter that
        # could promote would be a regex deciding to card — which is what the desk already does.
        if desk_route in CARD_ROUTES and want not in CARD_ROUTES \
                and carded_status not in ("TAKEN", "CLOSED"):
            STATS["pre_escalated"] += 1
            pre["escalate"] = True                        # not certain enough to act on alone
        else:
            return {"prefiltered": pre["cls"], "why": pre["why"], "llm": False}

    if not available():
        STATS["unavailable"] += 1
        return {"llm": False, "reason": "llm unavailable"}
    now = time.time()
    # Keyed on PROVIDER+text, not text alone. Measured bug (e2e, 2026-07-22): a text-only key made
    # the SAME sentence from a DIFFERENT provider look like a repost, so the second provider's
    # message was never routed at all. Providers quote each other constantly.
    h = LP._hash(f"{provider}\x00{text}")
    for k, v in list(_SEEN.items()):
        if now - v > _SEEN_TTL:
            _SEEN.pop(k, None)
    if h in _SEEN:
        STATS["dedup"] += 1
        return {"llm": False, "reason": "already routed"}
    try:
        window = LP.CONTEXT.window(provider, exclude_text=text)
        _JOBS.put_nowait({"text": text, "provider": provider, "desk_route": desk_route,
                          "card_id": card_id, "carded_status": carded_status,
                          "regex_sig": regex_sig, "channel": channel, "window": window,
                          "queued_ts": now})
        _SEEN[h] = now
        STATS["queued"] += 1
    except queue.Full:
        STATS["queue_full"] += 1
        return {"llm": False, "reason": "queue full"}      # a flood drops review, never stalls
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="llm-route", daemon=True)
        _worker.start()
    return {"llm": True, "queued": True}


def drain(limit=8, apply=True):
    """INTEGRATION POINT — call from the MAIN thread. Adjudicates finished routes and (unless
    apply=False) applies them through the bound desk adapter. Returns the verdicts."""
    out = []
    for _ in range(limit):
        try:
            r = _RESULTS.get_nowait()
        except queue.Empty:
            break
        route = r.get("route")
        if route is None:
            STATS["llm_failed"] += 1
            continue
        STATS["llm_ok"] += 1
        STATS["cls_" + str(route.get("message_class"))] += 1
        bv = None
        # Also resolve for MUST_ASK / ambiguous: the book is what decides whether the referent is
        # actually recoverable, so skipping it there would ask a vaguer question than necessary.
        if (route.get("refers_to_earlier_position") or not route.get("names_new_contract")
                or route.get("ambiguous_referent") or route.get("message_class") == "MUST_ASK"):
            try:
                bv = BOOK.resolve(r.get("provider"), r.get("text") or "",
                                  route.get("message_class"),
                                  hint_ticker=route.get("ticker"))
            except Exception:
                bv = None
        v = adjudicate(route, r.get("desk_route"), book_verdict=bv,
                       carded_status=r.get("carded_status"))
        v["latency_s"] = round((r.get("done_ts") or time.time()) - (r.get("queued_ts") or 0), 2)
        rec = {**{k: r.get(k) for k in ("text", "provider", "desk_route", "card_id")},
               "verdict": v, "route": route, "book": bv}
        STATS["act_" + v["action"]] += 1
        RECENT.append({"ts": time.time(), "provider": r.get("provider"),
                       "text": (r.get("text") or "")[:120], "cls": v.get("cls"),
                       "action": v["action"], "reason": v.get("reason")})
        if apply:
            try:
                _apply(rec)
            except Exception:
                pass
        out.append(rec)
    return out


def _apply(rec):
    """Effects. Every one of them goes through the bound adapter — this module never touches
    SIGNALS itself, so there is exactly one place that mutates desk state."""
    v, prov, text = rec["verdict"], rec.get("provider"), rec.get("text") or ""
    act = v["action"]
    if act == "DEMOTE" and DESK.get("retract") and rec.get("card_id"):
        DESK["retract"](rec["card_id"], v.get("reason"), v.get("cls"))
        STATS["applied_DEMOTE"] += 1
        return
    if act == "PROMOTE" and DESK.get("promote"):
        sig = v.get("sig") or {}
        canonical = _canonical(sig)
        if not canonical:
            return
        # HERE IS THE WHOLE SAFETY MODEL IN ONE LINE: we do not write a card. We write a SENTENCE
        # and hand it to ingest(), which parses it deterministically and then runs contract
        # verification, the price-mismatch guard, the junk gate, dedup and sizing on it. Nothing
        # downstream has to trust that this sentence came from a model.
        DESK["promote"](canonical, prov, {
            "action": "PROMOTE", "cls": v.get("cls"), "reason": v.get("reason"),
            "confidence": v.get("confidence"), "source": v.get("source"),
            "resolved_via": v.get("resolved_via"), "quotes": v.get("quotes"),
            "raw": text, "watch": bool(sig.get("watch")), "model": MODEL})
        STATS["applied_PROMOTE"] += 1
        return
    if act == "ASK" and DESK.get("alert"):
        cands = v.get("candidates") or []
        lines = ", ".join(f"{c['ticker']} {c['strike']:g}{c['right']}" for c in cands
                          if isinstance(c, dict)) if cands else ""
        DESK["alert"](f"❓ {prov}: Chip can't route this safely — {v.get('reason')}"
                      + (f"\ncandidates: {lines}" if lines else "")
                      + f"\n\"{text[:160]}\"", "Signal Desk — needs a human")
        STATS["applied_ASK"] += 1


def _canonical(sig):
    """A resolved trade rendered as text the DETERMINISTIC parser reads on its own. Reuses
    llm_parse._llm_canonical's shape; the YEAR is deliberately absent so server._resolve_expiry
    picks it (see llm_parse._strip_year)."""
    if not sig.get("ticker") or sig.get("strike") is None or not sig.get("type"):
        return None
    st = float(sig["strike"])
    st = str(int(st)) if abs(st - int(st)) < 1e-9 else f"{st:g}"
    out = f'{sig["ticker"]} {st}{sig["type"]}'
    exp = sig.get("expiry")
    if exp:
        m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', str(exp).strip())
        out += " " + (f"{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else str(exp))
    if sig.get("premium"):
        out += f' @{sig["premium"]}'
    return out


def status():
    return {"enabled": available(), "bound": _bound, "model": MODEL,
            "taxonomy": _taxonomy_note, "queued": _JOBS.qsize(),
            "pending": _RESULTS.qsize(), "stats": dict(STATS),
            "book": BOOK.stats(), "recent": list(RECENT)[-12:],
            "llm": (LP.status() if LP else None)}


_load_taxonomy()
