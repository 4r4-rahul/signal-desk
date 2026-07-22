#!/usr/bin/env python3
"""
LLM-ASSISTED PARSING LAYER — the reading-comprehension fallback behind the regex parser.

WHY THIS EXISTS
    prince_small posted two consecutive messages:
        MSG 1  "Watching SMCI 25 calls / July 31 / May even go this week expiry /
                First price target would be that 25.66 area off the 1H"
        MSG 2  "In SMCI july 31 / 1.67 per contract"
    The correct trade is SMCI 25C, expiry Jul 31, filled at $1.67, underlying target 25.66.
    MSG 2 CONTAINS NO STRIKE. It is only recoverable from MSG 1. No regex can do that — it
    needs reading comprehension plus conversational context. (_mask_dates in signal_engine
    already stops MSG 2 from INVENTING strike 31 out of "july 31"; this module is how the
    real strike gets recovered instead of just being dropped.)

THE CONTRACT (non-negotiable, in priority order)
    1. The LLM PROPOSES. It never commits. Everything it returns still goes through the
       existing price-mismatch guard, the junk gate, and the dedup sweep before carding.
    2. NOTHING IS TRUSTED THAT ISN'T CHECKABLE. Two independent, non-model checks gate every
       proposal:
         a) QUOTE GROUNDING — the model must return the verbatim substring it read each field
            from. If that quote is not literally present in the message window, the field is
            DROPPED. A hallucinated ticker cannot survive this (see _ground()).
         b) MARKET CORROBORATION — the caller quotes the proposed contract. A parse whose
            stated premium matches the real contract's live price is corroborated by a source
            that is not the model. That, not confidence, is what earns a live card.
    3. FAIL SAFE, NOT FAIL SILENT. Low confidence, anything unresolved, or any regex/LLM
       disagreement produces a CONFIRM-ME card (carded as WATCHING, zero risk, one-tap) —
       never a fabricated contract, and never a silent drop.
    4. FAIL OPEN ON INFRASTRUCTURE. No API key, no SDK, timeout, rate limit, malformed
       response, circuit open, budget spent -> behave EXACTLY as the desk does today.
    5. NEVER BLOCK THE DESK. server.py is a single-threaded http.server on :8787. An inline
       API call in the ingest path would stall every ingest AND every dashboard poll for the
       duration. So: ingest only ENQUEUES; a daemon worker thread does the network; results
       are applied back ON THE MAIN THREAD via drain() at the next request. No locks, no
       concurrent mutation of SIGNALS.

WHAT FEEDS WHAT
    This module deliberately does NOT re-implement things the desk already has:
      - a recovered WATCH becomes a WATCHING card, so the existing _apply_entry_fill()
        (server.py) upgrades it to a real fill when the provider posts "in at 1.67";
      - cross-day reply-tagging (manage.reply_parent_key) keeps owning replies;
      - sizing, stops, junk gate, dedup all stay exactly where they are.
"""
import json, os, queue, re, threading, time, hashlib
from collections import deque, defaultdict
from pathlib import Path

import deskpaths as _P

# ---------------------------------------------------------------- configuration

MODEL = "claude-haiku-4-5"
# WHY HAIKU 4.5: this is bounded structured extraction from <2 KB of text — the exact shape
# Haiku is built for. It supports strict tool use (so the schema is enforced, not requested),
# costs $1/$5 per MTok against Opus 4.8's $5/$25, and is the fastest tier — which matters even
# off the hot path, because an options signal decays while we deliberate. It is also already
# the model manage.classify_llm uses, so the desk keeps ONE model to reason about. Escalating
# to Sonnet 5 / Opus 4.8 would buy accuracy we cannot bank anyway: the guards downstream, not
# the model, decide what cards.

MAX_TOKENS = 700
TIMEOUT_S = 12.0          # a signal older than ~12s of extra latency is already stale
MAX_RETRIES = 1           # fail open fast; do NOT retry-storm a live desk
QUEUE_MAX = 64            # bounded: a message flood drops LLM review, never memory
DAILY_CALL_CAP = 400      # hard money stop. See COST estimate in llm_eval.py.
BREAKER_TRIPS = 4         # consecutive failures before the circuit opens
BREAKER_COOLDOWN_S = 600  # ...and how long it stays open

# Conversational context window. JUSTIFICATION — measured, not guessed
# (`python3 llm_eval.py context-stats` over 676 real gaps in logs/inbound.log):
#     p50 7.8 min | p75 32.5 min | p90 151.7 min | p95 963.5 min
# Two separate limits, because they do different jobs:
#   CTX_MESSAGES=6  bounds how much OTHER material enters the prompt. The referent of an
#     anaphoric entry ("In SMCI july 31") is the same provider's IMMEDIATELY PRECEDING message
#     in every case observed, so 6 is ~6x headroom. Going wider is an ACTIVE HARM, not just
#     waste: the failure mode we care about is fabrication, and every extra unrelated contract
#     in the window is one more plausible thing to fabricate from. Fewer wrong answers
#     available is itself a safety property.
#   CTX_MINUTES=240 bounds staleness. It covers ~91% of consecutive same-provider gaps and,
#     more importantly, spans a single trading session — a provider who posts a watch at the
#     open and fills it after lunch still resolves. It is deliberately shorter than the 24h
#     window _apply_entry_fill uses for watch->fill, because that path has a real fill price to
#     match against and this one does not.
# Cost of this shape: ~450 input tokens/call. See `python3 llm_eval.py cost`.
CTX_MESSAGES = 6
CTX_MINUTES = 240
CTX_CHARS = 1500

CONFIDENCE_CARD = 0.80    # below this, a proposal can only ever be a CONFIRM-ME card
MISMATCH_X = 3.0          # stated-vs-market ratio that means "this contract is not that trade"

_CTX_FILE = _P.CHANNELS / "_msg_context.json"

# ---------------------------------------------------------------- the strict schema
# Every field is INDEPENDENTLY NULLABLE — "I don't know the strike" must be expressible, and
# is the single most important thing this schema can say. anyOf[..., null] rather than a bare
# type array because anyOf is explicitly supported under strict tool use.

def _nullable(t, **kw):
    return {"anyOf": [dict({"type": t}, **kw), {"type": "null"}]}


PARSE_TOOL = {
    "name": "record_parse",
    "description": (
        "Record the options trade the provider is describing in the LAST message. "
        "Use the earlier messages ONLY to resolve things the last message leaves implicit."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ticker":   _nullable("string"),
            "strike":   _nullable("number"),
            "right":    {"anyOf": [{"type": "string", "enum": ["C", "P"]}, {"type": "null"}]},
            "expiry":   _nullable("string"),
            "premium":  _nullable("number"),
            "stop":     _nullable("number"),
            "stop_kind": {"anyOf": [{"type": "string",
                                     "enum": ["premium", "underlying", "percent"]},
                                    {"type": "null"}]},
            "targets":  {"type": "array", "items": {"type": "number"}},
            "target_kind": {"anyOf": [{"type": "string", "enum": ["underlying", "premium"]},
                                      {"type": "null"}]},
            "intent":   {"anyOf": [{"type": "string",
                                    "enum": ["ENTRY", "WATCH", "MANAGEMENT", "COMMENTARY"]},
                                   {"type": "null"}]},
            "confidence": {"type": "number"},
            "unresolved": {"type": "array", "items": {"type": "string"}},
            "resolved_from_context": {"type": "array", "items": {"type": "string"}},
            "quotes": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticker":  _nullable("string"),
                    "strike":  _nullable("string"),
                    "expiry":  _nullable("string"),
                    "premium": _nullable("string"),
                },
                "required": ["ticker", "strike", "expiry", "premium"],
            },
        },
        "required": ["ticker", "strike", "right", "expiry", "premium", "stop", "stop_kind",
                     "targets", "target_kind", "intent", "confidence", "unresolved",
                     "resolved_from_context", "quotes"],
    },
}

SYSTEM = """You extract options trades from a copy-trading Discord feed. You are the fallback \
behind a regex parser that already handled the easy messages; you only see the hard ones.

You are given a provider's recent messages in order. Extract ONLY the trade described in the \
LAST message. Earlier messages exist for one reason: to resolve what the last message leaves \
implicit (a strike, an expiry, a ticker it refers to by context).

RULES
1. A field you cannot determine MUST be null and MUST be listed in `unresolved`. Guessing is \
the worst thing you can do here — a wrong contract puts a fake trade on a live trading desk, \
while a null just asks a human to glance at it. Never infer a strike from an expiry date, a \
price target, a stop, or a premium.
2. `strike` is the option's strike price. `premium` is what ONE CONTRACT costs (usually \
$0.10-$20). An underlying price target ("first target is that 25.66 area") is NEITHER — it \
goes in `targets`. "july 31" / "7/31" is an EXPIRY, never a strike.
3. `quotes` must contain the VERBATIM substring you read each value from, copied exactly from \
the messages, including from an earlier message when that is where it came from. If you cannot \
point at text, the field is null.
4. `resolved_from_context` lists the field names whose value came from an EARLIER message \
rather than the last one.
5. `intent`: ENTRY = they are in / are entering now. WATCH = eyeing it, stating a planned \
price, resting a limit order — not filled. MANAGEMENT = trimming, exiting, stopping out, \
moving a stop, reporting P&L on an existing position. COMMENTARY = everything else.
6. `confidence` is 0-1 for the parse AS A WHOLE. Be honest and be harsh. Below 0.8 a human \
has to confirm this by hand, which is the correct outcome when you are unsure.
7. AMBIGUOUS REFERENT: if the last message refers to a position without naming it ("added 1.05", \
"in at 0.93") and MORE THAN ONE distinct contract in the earlier messages could be what it means, \
you must NOT pick one. Set the ambiguous fields to null, list them in `unresolved`, and set \
confidence below 0.5. Picking the most recent one is a guess, and a guess here attaches a fill to \
the wrong position. Only resolve from context when exactly one contract is a plausible referent.

Example of the case you exist for:
  [earlier] "Watching SMCI 25 calls / July 31 / First price target would be that 25.66 area"
  [last]    "In SMCI july 31 / 1.67 per contract"
  -> ticker SMCI, strike 25, right C, expiry 2026-07-31, premium 1.67, targets [25.66],
     intent ENTRY, resolved_from_context ["strike","right","targets"],
     quotes.strike "Watching SMCI 25 calls".
The strike is 25 (from the earlier message). It is NOT 31 — that is the day of the month."""


# ---------------------------------------------------------------- trigger conditions
# The LLM must NOT run on every message. ~70% of inbound traffic parses fine or is noise;
# calling on all of it is waste, latency and a bigger blast radius for a model mistake.

_TICKERISH = re.compile(r"\b[A-Za-z]{1,5}\b")
_PRICEISH = re.compile(r"(?<![\d.])(?:\.\d{1,2}|\d{1,4}\.\d{1,2})\b")
_PUTWORD = re.compile(r"\bputs?\b|\d\s*p\b", re.I)
_CALLWORD = re.compile(r"\bcalls?\b|\d\s*c\b", re.I)


def triggers(text, regex_sig=None, carded=False, market_mid=None, is_watch=None,
             maybe_entry=False):
    """Which hard-case conditions this message hits. Empty list -> the regex owns it, no call.

    regex_sig   : signal_engine.parse_signal() output (or None if it parsed nothing)
    carded      : whether the desk produced a card
    market_mid  : live mid of the contract the regex proposed, if already quoted
    is_watch    : signal_engine.is_watch(text)
    maybe_entry : server._maybe_missed_entry(text, classified)
    """
    t = text or ""
    low = t.lower()
    hits = []

    # T1 — the safety net already fired: smells like an entry, parsed to nothing.
    if maybe_entry and not carded:
        hits.append("unparsed_possible_entry")

    # T2 — a probable entry with no strike. This is the SMCI MSG-2 shape: after _mask_dates
    # there is no strike to be had from this message alone, so it can only come from context.
    if not regex_sig or not regex_sig.get("strike"):
        has_prem = bool(_PRICEISH.search(t))
        entryish = bool(re.search(r"\b(in|filled|took|taking|bought|buying|added|adding|"
                                  r"grabbed|entered|entering|watching|watch)\b", low))
        if has_prem and entryish and not maybe_entry:
            hits.append("no_strike_on_probable_entry")

    # T3 — price-mismatch: the live contract is nowhere near the stated premium, so the
    # contract we resolved is not the contract they named. (This is the guard that caught the
    # fabricated SMCI 31C: stated 1.67 vs market 0.21.)
    if regex_sig and market_mid and regex_sig.get("premium"):
        p, m = float(regex_sig["premium"]), float(market_mid)
        if p > 0 and (m > p * MISMATCH_X or m * MISMATCH_X < p):
            hits.append("price_mismatch")

    # T4 — watch/entry ambiguity: watch language AND fill language in the same message. The
    # difference is a live position vs a card that just sits there, so guessing is expensive.
    if is_watch is not None and regex_sig:
        firm = bool(re.search(r"\b(filled|i'?m in|got in|im in)\b", low))
        if is_watch and firm:
            hits.append("watch_entry_ambiguous")

    # T5 — the regex and the message disagree on their face.
    if regex_sig:
        # right defaulted to CALL with no c/p word anywhere, but the text says "put"
        if regex_sig.get("assumed_call") and _PUTWORD.search(t):
            hits.append("assumed_call_but_text_says_put")
        elif regex_sig.get("type") == "C" and _PUTWORD.search(t) and not _CALLWORD.search(t):
            hits.append("right_disagrees_with_text")
        elif regex_sig.get("type") == "P" and _CALLWORD.search(t) and not _PUTWORD.search(t):
            hits.append("right_disagrees_with_text")
        # the ticker we resolved does not appear in the message at all
        tk = (regex_sig.get("ticker") or "").upper()
        if tk and not re.search(rf"\b{re.escape(tk)}\b", t.upper()):
            hits.append("ticker_not_in_text")
    return hits


# ---------------------------------------------------------------- conversation context

class _Context:
    """Bounded per-provider ring of recent raw messages. Memory-first (this is a hot path),
    persisted best-effort so a desk restart between MSG 1 and MSG 2 doesn't lose the referent."""

    def __init__(self, per_provider=40):
        self._d = defaultdict(lambda: deque(maxlen=per_provider))
        self._dirty = 0
        self._load()

    def note(self, provider, text, ts=None, msg_id=None):
        if not provider or not text:
            return
        self._d[provider].append({"ts": ts or time.time(), "text": text[:600], "msg_id": msg_id})
        self._dirty += 1
        if self._dirty >= 10:
            self.save()

    def window(self, provider, exclude_text=None, n=CTX_MESSAGES, minutes=CTX_MINUTES):
        """The last n messages from this provider inside the time window, oldest first."""
        cut = time.time() - minutes * 60
        out = []
        for m in reversed(self._d.get(provider) or ()):
            if m["ts"] < cut:
                break
            if exclude_text is not None and m["text"] == exclude_text and not out:
                continue                                   # skip the message being parsed
            out.append(m)
            if len(out) >= n:
                break
        out.reverse()
        # oldest-first, trimmed to a char budget from the END (recent matters most)
        while sum(len(m["text"]) for m in out) > CTX_CHARS and len(out) > 1:
            out.pop(0)
        return out

    def save(self):
        try:
            import safe_io
            payload = {k: list(v) for k, v in self._d.items() if v}
            safe_io.safe_write_json(_CTX_FILE, payload, min_keep_ratio=0.0)   # regenerable cache
            self._dirty = 0
        except Exception:
            pass

    def _load(self):
        try:
            for k, v in json.loads(Path(_CTX_FILE).read_text()).items():
                cut = time.time() - 86400
                self._d[k].extend([m for m in v if m.get("ts", 0) > cut])
        except Exception:
            pass


CONTEXT = _Context()


def note_message(provider, text, ts=None, msg_id=None):
    """INTEGRATION POINT: call once per inbound message, before parsing."""
    CONTEXT.note(provider, text, ts, msg_id)


# ---------------------------------------------------------------- the client

def _env(k):
    v = os.environ.get(k)
    if v:
        return v
    try:
        for ln in (_P.CODE / ".env").read_text().splitlines():
            if ln.startswith(k + "="):
                return ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


class _State:
    def __init__(self):
        self.fails = 0
        self.open_until = 0.0
        self.calls_today = 0
        self.day = time.strftime("%Y-%m-%d")
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_error = None

    def budget_ok(self):
        d = time.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.calls_today, self.tokens_in, self.tokens_out = d, 0, 0, 0
        return self.calls_today < DAILY_CALL_CAP

    def breaker_ok(self):
        return time.time() >= self.open_until

    def ok(self, usage=None):
        self.fails = 0
        self.calls_today += 1
        if usage:
            self.tokens_in += getattr(usage, "input_tokens", 0) or 0
            self.tokens_out += getattr(usage, "output_tokens", 0) or 0

    def fail(self, err):
        self.fails += 1
        self.last_error = str(err)[:200]
        self.calls_today += 1
        if self.fails >= BREAKER_TRIPS:
            self.open_until = time.time() + BREAKER_COOLDOWN_S


STATE = _State()
_client = None
_client_err = None


def _get_client():
    global _client, _client_err
    if _client is not None or _client_err is not None:
        return _client
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        _client_err = "no ANTHROPIC_API_KEY"
        return None
    try:
        import anthropic
        _client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
    except Exception as e:                                  # SDK missing / broken -> fail open
        _client_err = f"sdk unavailable: {e}"
    return _client


def available():
    return _get_client() is not None and STATE.breaker_ok() and STATE.budget_ok()


def _render(window, text):
    lines = []
    for i, m in enumerate(window):
        age = max(0, int((time.time() - m["ts"]) / 60))
        lines.append(f"[earlier, {age} min ago]\n{m['text']}")
    lines.append(f"[last message — extract THIS one]\n{text}")
    return "\n\n".join(lines)


def call_tool(system, tool, prompt, model=None, max_tokens=None):
    """SHARED PRIMITIVE (used by llm_parse.call and by llm_route). One forced-tool call against the
    desk's ONE client, ONE circuit breaker and ONE daily budget.

    Why this is a shared function rather than a second client in the router: the breaker, the daily
    call cap and the token counters are money-and-safety state. Two modules each with their own copy
    would each be allowed the full budget, and a provider outage would have to trip two breakers
    before the desk stopped calling. Returns the tool input dict, or None on ANY failure (fail open).
    Blocking — worker threads only."""
    c = _get_client()
    if c is None or not STATE.breaker_ok() or not STATE.budget_ok():
        return None
    try:
        r = c.messages.create(
            model=model or MODEL, max_tokens=max_tokens or MAX_TOKENS, system=system,
            tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        STATE.ok(getattr(r, "usage", None))
    except Exception as e:
        STATE.fail(e)
        return None
    try:
        blk = next(b for b in r.content if b.type == "tool_use")
    except Exception as e:                                  # malformed / refusal / empty
        STATE.fail(f"no tool_use: {e}")
        return None
    out = dict(blk.input)
    u = getattr(r, "usage", None)
    out["_usage"] = {"in": getattr(u, "input_tokens", 0) or 0,
                     "out": getattr(u, "output_tokens", 0) or 0,
                     "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                     "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}
    return out


def ground(raw, corpus, fields, quotes=None):
    """SHARED PRIMITIVE. Null out every field whose claimed source quote is not literally present in
    the exact bytes we sent. See _ground() for the full rationale — this is the same test, exposed
    for callers with a different field set. Returns the list of dropped field names.

    `fields` maps output-field-name -> quotes-key. Mutates `raw` in place."""
    hay = _norm(corpus)
    q = quotes if quotes is not None else (raw.get("quotes") or {})
    dropped = []
    for field, qkey in fields.items():
        if raw.get(field) is None:
            continue
        s = _norm(q.get(qkey))
        if not s or s not in hay:
            dropped.append(field)
            raw[field] = None
    return dropped


def numeric_in_text(value, corpus):
    """SHARED PRIMITIVE. A value can be quoted correctly and still be a transcription error, so the
    literal number must also appear. Same check _ground() applies to strike/premium."""
    if value is None:
        return True
    try:
        s = f"{float(value):g}"
    except (TypeError, ValueError):
        return False
    return s in re.sub(r"[,$]", "", corpus or "")


def mark_seen(text):
    """SHARED PRIMITIVE. Register a text as already paid for, so the OTHER module's submit() skips it.
    Both layers read the same message stream; without this the desk pays twice for one message."""
    if text:
        _seen[_hash(text)] = time.time()


def already_seen(text):
    h = _hash(text or "")
    v = _seen.get(h)
    return bool(v and time.time() - v <= _SEEN_TTL)


def call(text, provider=None, window=None, why=None):
    """One extraction call. Returns a parse dict, or None on ANY failure (fail open).
    Blocking — only the worker thread should call this."""
    c = _get_client()
    if c is None or not STATE.breaker_ok() or not STATE.budget_ok():
        return None
    if window is None:
        window = CONTEXT.window(provider, exclude_text=text)
    prompt = _render(window, text)
    try:
        r = c.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
            tools=[PARSE_TOOL], tool_choice={"type": "tool", "name": "record_parse"},
            messages=[{"role": "user", "content": prompt}],
        )
        STATE.ok(getattr(r, "usage", None))
    except Exception as e:
        STATE.fail(e)
        return None
    try:
        blk = next(b for b in r.content if b.type == "tool_use")
        raw = dict(blk.input)
    except Exception as e:                                  # malformed / refusal / empty
        STATE.fail(f"no tool_use: {e}")
        return None
    return _ground(raw, prompt, window, text, provider, why)


# ---------------------------------------------------------------- grounding (non-model check)

_FIELD_Q = {"ticker": "ticker", "strike": "strike", "expiry": "expiry", "premium": "premium"}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _ground(raw, corpus, window, text, provider, why):
    """Drop every field whose claimed source quote is not literally in the text we supplied.

    This is the anti-fabrication check, and it does not trust the model to self-report: it is a
    substring test against the exact bytes we sent. A model that invents "SMCI 31 calls" cannot
    produce a quote for it, and the field is nulled and moved to `unresolved`. It is deliberately
    stricter than the model's own confidence, which is a self-assessment and therefore not
    evidence."""
    hay = _norm(corpus)
    quotes = raw.get("quotes") or {}
    dropped = []
    for field, qkey in _FIELD_Q.items():
        if raw.get(field) is None:
            continue
        q = _norm(quotes.get(qkey))
        if not q or q not in hay:
            dropped.append(field)
            raw[field] = None
    # a value can be quoted correctly and still be a transcription error — check the literal
    # number actually appears somewhere for numeric fields
    for field in ("strike", "premium"):
        v = raw.get(field)
        if v is None:
            continue
        s = f"{float(v):g}"
        if s not in re.sub(r"[,$]", "", corpus):
            dropped.append(field + ":not_in_text")
            raw[field] = None
    unresolved = list(raw.get("unresolved") or [])
    for d in dropped:
        base = d.split(":")[0]
        if base not in unresolved:
            unresolved.append(base)
    raw["unresolved"] = unresolved
    raw["ungrounded_dropped"] = dropped
    raw["_meta"] = {"provider": provider, "why": why or [], "model": MODEL,
                    "ctx_msgs": len(window), "text": text[:200]}
    try:
        raw["confidence"] = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except Exception:
        raw["confidence"] = 0.0
    if dropped:
        raw["confidence"] = min(raw["confidence"], 0.5)     # ungrounded -> can never auto-card
    return raw


# ---------------------------------------------------------------- adjudication

_CARD_FIELDS = ("ticker", "strike", "right", "premium", "expiry")
_YMD_RE = re.compile(r'^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$')


def _strip_year(exp):
    """Drop the YEAR from an LLM expiry, keeping MM/DD, so server._resolve_expiry picks the year
    deterministically (nearest matching calendar date).

    THE MODEL MUST NEVER PICK THE YEAR. Measured: on "In SMCI july 31" with a prior watch message in
    context, haiku returned expiry 2025-07-31 -- the wrong year, in 2026 -- while the SAME message with
    NO context returned 2026-07-31 correctly. Providers virtually never state a year, so the model is
    guessing, and a wrong year is a contract that does not exist: IBKR rejects it every lap and the
    signal becomes a dead card with no quote, no path and no outcome (22 such signals already exist from
    the MM/DD -> next-year bug fixed in 9e62eac).

    This is the honesty contract applied to the LLM: it may report what the provider SAID ("july 31");
    the deterministic resolver decides what that means. 0DTE/NDTE and bare MM/DD pass through untouched."""
    if not exp:
        return exp
    m = _YMD_RE.match(str(exp))
    return f"{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else exp


def adjudicate(llm, regex_sig=None):
    """Turn a grounded proposal + the regex's opinion into a DECISION. No network, no market
    data, no side effects — so it is directly unit-testable, which is why the guards live in
    the caller and the reasoning lives here.

    action:
      DROP     — nothing actionable (commentary / management / no contract).
      ANNOTATE — regex already carded; the LLM disagrees. Surface BOTH on the card. Never
                 silently overwrite a card the regex produced.
      CONFIRM  — a proposal a human must approve. Cards as WATCHING (zero risk) so the desk's
                 existing _apply_entry_fill() can still upgrade it on a real fill.
      CARD     — complete, high-confidence, no disagreement. STILL subject to the caller's
                 market-corroboration check + junk gate before it becomes a real card.
    """
    if not llm:
        return {"action": "DROP", "reason": "no parse"}
    intent = llm.get("intent")
    if intent in (None, "COMMENTARY", "MANAGEMENT"):
        return {"action": "DROP", "reason": f"intent={intent}", "llm": llm}

    missing = [f for f in _CARD_FIELDS if f in ("ticker", "strike", "right") and llm.get(f) is None]
    unresolved = list(llm.get("unresolved") or [])
    conf = float(llm.get("confidence") or 0)

    disagreements = []
    if regex_sig:
        pairs = {
            "ticker": (regex_sig.get("ticker"), llm.get("ticker")),
            "strike": (_num(regex_sig.get("strike")), _num(llm.get("strike"))),
            "right": (regex_sig.get("type"), llm.get("right")),
            "premium": (_num(regex_sig.get("premium")), _num(llm.get("premium"))),
        }
        for f, (a, b) in pairs.items():
            if a is None or b is None:
                continue                       # one side silent is not a disagreement
            if isinstance(a, float) and isinstance(b, float):
                if abs(a - b) > 1e-6:
                    disagreements.append({"field": f, "regex": a, "llm": b})
            elif str(a).upper() != str(b).upper():
                disagreements.append({"field": f, "regex": a, "llm": b})

    sig = {
        "provider": (llm.get("_meta") or {}).get("provider"),
        "ticker": llm.get("ticker"),
        "strike": None if llm.get("strike") is None else _fmt_strike(llm["strike"]),
        "type": llm.get("right"),
        "expiry": _strip_year(llm.get("expiry")),
        "dte": 0 if str(llm.get("expiry") or "").upper() == "0DTE" else None,
        "premium": llm.get("premium"),
        "lotto": False,
        "assumed_call": False,
        "stated_stop": ({"value": llm["stop"], "kind": llm.get("stop_kind") or "premium"}
                        if llm.get("stop") is not None else None),
        "watch": intent == "WATCH",
        "via_llm": True,
    }
    out = {"sig": sig, "llm": llm, "disagreements": disagreements,
           "unresolved": unresolved, "confidence": conf,
           "targets": llm.get("targets") or [], "target_kind": llm.get("target_kind"),
           "resolved_from_context": llm.get("resolved_from_context") or []}

    # THE REGEX KEEPS WHAT IT WON. If the deterministic parser already produced a card, the LLM
    # may only ANNOTATE it — never replace it, never downgrade it to a question. Letting a
    # low-confidence model opinion demote a working card would make the desk worse on the ~90%
    # of traffic the regex already handles, to chase the ~10% it doesn't. (The eval caught this:
    # allowing replacement dropped ticker accuracy from 98.4% to 96.1% on market-corroborated
    # cards.) The one thing worth saying about an existing card is "these two disagree".
    if regex_sig:
        if disagreements:
            out.update(action="ANNOTATE",
                       reason="regex/LLM disagree: " +
                              ", ".join(f"{d['field']} {d['regex']}!={d['llm']}"
                                        for d in disagreements))
        else:
            out.update(action="DROP", reason="regex card stands; LLM adds nothing")
        return out

    if missing:
        # No contract at all -> ASK. Never assemble a partial contract into a card.
        out.update(action="CONFIRM", reason=f"incomplete: {', '.join(missing)}")
        return out
    if unresolved or conf < CONFIDENCE_CARD:
        out.update(action="CONFIRM",
                   reason=(f"confidence {conf:.2f}" if conf < CONFIDENCE_CARD
                           else "unresolved: " + ", ".join(unresolved)))
        return out
    out.update(action="CARD", reason=f"complete, confidence {conf:.2f}")
    return out


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _fmt_strike(v):
    f = float(v)
    return str(int(f)) if abs(f - int(f)) < 1e-9 else f"{f:g}"


def corroborated(sig, market_mid):
    """The ONLY test that lets a model proposal become a live card without a human. It is not a
    model output: it asks the option chain whether the contract the model named actually trades
    near the price the provider stated. Returns (bool, note).

    No quote at all -> NOT corroborated (unknown is not confirmation).
    No stated premium -> NOT corroborated (nothing to check it against)."""
    p, m = _num(sig.get("premium")), _num(market_mid)
    if not p or not m:
        return False, "no live quote to corroborate against"
    if m > p * MISMATCH_X or m * MISMATCH_X < p:
        return False, f"stated ${p:.2f} vs market ${m:.2f} — not this contract"
    return True, f"market ${m:.2f} corroborates stated ${p:.2f}"


# ---------------------------------------------------------------- async plumbing
# server.py is a single-threaded http.server. The worker does NETWORK ONLY; every mutation of
# desk state happens back on the main thread in drain(). No locks needed, no torn writes.

_JOBS = queue.Queue(maxsize=QUEUE_MAX)
_RESULTS = queue.Queue()
_worker = None
_seen = {}                      # text hash -> ts, so a repost doesn't pay twice
_SEEN_TTL = 3600


def _hash(t):
    return hashlib.md5((t or "").encode()).hexdigest()


def _run():
    while True:
        job = _JOBS.get()
        try:
            parse = call(job["text"], provider=job.get("provider"),
                         window=job.get("window"), why=job.get("why"))
            _RESULTS.put({**job, "parse": parse, "done_ts": time.time()})
        except Exception as e:                             # a worker must never die
            _RESULTS.put({**job, "parse": None, "error": str(e)[:200]})
        finally:
            _JOBS.task_done()


def submit(text, provider=None, why=None, card_id=None, extra=None):
    """INTEGRATION POINT: enqueue a hard case. Returns True if queued. NEVER blocks, never
    raises. The caller carries on and cards exactly what it would have carded today."""
    global _worker
    if not available() or not text:
        return False
    h = _hash(text)
    now = time.time()
    for k, v in list(_seen.items()):
        if now - v > _SEEN_TTL:
            _seen.pop(k, None)
    if h in _seen:
        return False
    try:
        window = CONTEXT.window(provider, exclude_text=text)
        _JOBS.put_nowait({"text": text, "provider": provider, "why": why or [],
                          "card_id": card_id, "extra": extra or {}, "window": window,
                          "queued_ts": now})
        _seen[h] = now
    except queue.Full:
        return False                                       # flood -> skip review, never stall
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="llm-parse", daemon=True)
        _worker.start()
    return True


def drain(limit=8):
    """INTEGRATION POINT: call from the MAIN thread. Returns finished {job, parse, decision}."""
    out = []
    for _ in range(limit):
        try:
            r = _RESULTS.get_nowait()
        except queue.Empty:
            break
        r["decision"] = adjudicate(r.get("parse"), (r.get("extra") or {}).get("regex_sig"))
        out.append(r)
    return out


def status():
    return {"enabled": _get_client() is not None, "error": _client_err or STATE.last_error,
            "model": MODEL, "calls_today": STATE.calls_today, "cap": DAILY_CALL_CAP,
            "tokens_in": STATE.tokens_in, "tokens_out": STATE.tokens_out,
            "breaker_open": not STATE.breaker_ok(), "queued": _JOBS.qsize(),
            "pending_results": _RESULTS.qsize()}
