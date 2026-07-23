#!/usr/bin/env python3
"""
chip_thesis.py — Phase 2: the agentic thesis layer.

An LLM proposes a FALSIFIABLE directional thesis; it lands in the immutable playbook as a THESIS event;
Phase 3 resolves it against real bars at several horizons and writes the OUTCOMEs. The LLM never touches
money and never gates a trade — it makes a prediction that the record grades.

WHY THIS SHAPE, and it is the whole point:

  A thesis that cannot be graded is worthless — worse than worthless, because fluent prose feels like
  insight. So the schema REQUIRES the four things that make a claim falsifiable:
      direction · horizon · target · invalidation
  plus the spot price AT PROPOSAL TIME, which is what makes resolution possible at all. "QQQ looks strong"
  is rejected by the schema. "QQQ LONG, 240 min, +0.35%, invalid below 704.10, from 705.20" is a bet the
  record can settle.

  This is deliberately an EXPERIMENT, not a feature. The question it exists to answer is: does an LLM
  thesis carry forward drift that a coin flip does not? Five candidate signals on this desk have already
  measured zero. The honest prior is that this one does too — and the design must be able to SAY so, which
  is why every thesis is recorded before its outcome is known and can never be quietly withdrawn.

CONTRACT (inherited from the rest of the desk, non-negotiable):
  · The LLM proposes; deterministic code resolves. No model output selects an instrument or sizes anything.
  · Never blocks: proposal runs off the request path, ingest is untouched.
  · Fail-open: no key, no SDK, timeout, bad JSON -> no thesis, desk behaves exactly as before.
  · Every proposal is written BEFORE its outcome exists, so the sample cannot be curated after the fact.
"""
import json, threading, time
from datetime import datetime, timezone

HORIZONS_MIN = (20, 60, 240, 1950)          # 20m, 1h, 4h(~session), ~5 sessions — minutes vs days
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 900
DAILY_CAP = 120                             # 74 slots/day + headroom. At 60 a 74-call day truncated at
                                            # ~12:40 ET, producing a MORNING-BIASED sample — the exact
                                            # time-of-day bias the no-blackout decision exists to avoid.
PER_TICKER_CAP = 45                         # one ticker cannot starve the other
EXPERIMENT = "v1-10min-grid"                # stamped on every thesis: a later prompt/model change becomes
                                            # a separable COHORT instead of silent contamination
MIN_CONVICTION = 0.55                       # below this we record it but flag it as low-conviction

_STATE = {"day": None, "calls": 0, "by_ticker": {}}
_LOCK = threading.Lock()

# Strict tool schema. Every field that makes the claim falsifiable is REQUIRED — the model cannot return
# a vibe. direction NONE is explicitly allowed and is the correct answer most of the time.
THESIS_TOOL = {
    "name": "record_thesis",
    "description": ("Record a falsifiable directional thesis for one instrument, or NONE if the evidence "
                    "does not support a directional call. NONE is a valid and common answer."),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "direction":     {"type": "string", "enum": ["LONG", "SHORT", "NONE"]},
            "horizon_min":   {"type": "integer", "description": "minutes the thesis needs to play out"},
            "target_pct":    {"type": "number", "description": "expected underlying move %, signed"},
            "invalidation_pct": {"type": "number", "description": "underlying move % that proves it wrong"},
            "conviction":    {"type": "number", "description": "0..1"},
            "regime":        {"type": "string", "enum": ["trend", "range", "reversal", "event", "unclear"]},
            "reasoning":     {"type": "string", "description": "the causal claim, one or two sentences"},
            "evidence":      {"type": "array", "items": {"type": "string"},
                              "description": "the specific observations relied on"},
        },
        "required": ["direction", "horizon_min", "target_pct", "invalidation_pct",
                     "conviction", "regime", "reasoning", "evidence"],
    },
}

SYSTEM = """You are a disciplined markets analyst producing FALSIFIABLE theses, not commentary.

You will be given the current state of one instrument. Produce a directional thesis ONLY if the evidence
genuinely supports one. Return direction NONE otherwise — and NONE is the right answer most of the time,
because most minutes of most days carry no forecastable drift.

Hard rules:
- Your claim must be settleable against price alone. Give a horizon, a target and an invalidation.
- Do NOT justify a call with facts already reflected in the price. Public information is priced.
- Do NOT manufacture a narrative to fit a move that has already happened. If the move is done, say NONE.
- Prefer NONE over a low-conviction guess. A wrong confident thesis is worse than no thesis, because it
  will be graded and it will pollute the record.
- Conviction is your honest probability that the target is reached before the invalidation."""


def _today():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _budget_ok(ticker=None):
    with _LOCK:
        d = _today()
        if _STATE["day"] != d:
            _STATE["day"], _STATE["calls"], _STATE["by_ticker"] = d, 0, {}
        if _STATE["calls"] >= DAILY_CAP:
            return False
        if ticker and _STATE["by_ticker"].get(ticker, 0) >= PER_TICKER_CAP:
            return False
        _STATE["calls"] += 1
        if ticker:
            _STATE["by_ticker"][ticker] = _STATE["by_ticker"].get(ticker, 0) + 1
        return True


def _render(ticker, ctx):
    """The market state the model reasons over. Deliberately factual and compact — no leading language, no
    'this looks bullish', because a prompt that hints at an answer manufactures one."""
    L = [f"instrument: {ticker}", f"spot: {ctx.get('spot')}", f"time_et: {ctx.get('time_et')}"]
    for k in ("session_min", "vwap", "ema9", "ema21", "ema135", "ema315", "rsi",
              "bb_up", "bb_low", "day_range_pct", "rel_volume", "prior_close", "open"):
        if ctx.get(k) is not None:
            L.append(f"{k}: {ctx[k]}")
    if ctx.get("recent_closes"):
        L.append(f"last_20_1m_closes: {ctx['recent_closes'][-20:]}")
    if ctx.get("news"):
        L.append(f"news_state: {ctx['news']}")
    return "\n".join(L)


def propose(ticker, ctx, *, actor="llm", signal_id=None):
    """Ask for a thesis and RECORD it immutably, outcome-blind. Returns the thesis dict or None.

    Recording happens BEFORE any outcome is known, which is the property that makes the eventual hit-rate
    honest: nothing can be added or removed once the market has moved."""
    try:
        # check the CHEAP disqualifier before spending a call: propose("SPY", {}) previously burned one
        # paid request on a guaranteed discard, and budget bounds call COUNT so that is real money.
        if _num(ctx.get("spot")) is None:
            return None
        import llm_parse as LP
        if not LP.available() or not _budget_ok(ticker):
            return None
        raw = LP.call_tool(SYSTEM, THESIS_TOOL, _render(ticker, ctx), model=MODEL, max_tokens=MAX_TOKENS)
        if not raw or not isinstance(raw, dict):
            return None
    except Exception:
        return None

    # EVERYTHING below is inside one try. The contract is "never raises into the caller"; the first
    # version only wrapped the API call, so a model returning conviction as a STRING, reasoning as an int,
    # or evidence as a dict raised TypeError/KeyError straight out. strict:True on a tool schema is not a
    # guarantee the API enforces every type on every model, and a partial/refusal block is exactly what
    # defensive parsing exists for. Coerce, never trust.
    try:
        spot = _num(ctx.get("spot"))
        if spot is None or spot <= 0:
            return None
        direction = raw.get("direction")
        if direction not in ("LONG", "SHORT", "NONE"):
            return None
        conv = _num(raw.get("conviction")) or 0.0
        th = {
            "ticker": str(ticker), "spot_at_proposal": float(spot),
            "direction": direction,
            "horizon_min": _int(raw.get("horizon_min")),
            "target_pct": _num(raw.get("target_pct")),
            "invalidation_pct": _num(raw.get("invalidation_pct")),
            "conviction": conv, "regime": str(raw.get("regime") or "unclear"),
            "reasoning": _str(raw.get("reasoning"))[:600],
            "evidence": [_str(x)[:200] for x in (raw.get("evidence") or [])][:6]
                        if isinstance(raw.get("evidence"), (list, tuple)) else [],
            "low_conviction": conv < MIN_CONVICTION,
            "proposed_utc": datetime.now(timezone.utc).isoformat(),
            "model": MODEL, "horizons_to_grade": list(HORIZONS_MIN),
            "experiment": EXPERIMENT, "slot_id": ctx.get("slot_id"),
            # SNAPSHOT the conditioning variables. The experiment is deliberately UNCONDITIONAL, but
            # recording the covariates turns one unconditional sample into an unlimited set of post-hoc
            # conditional tests. Costs nothing now; unrecoverable if skipped.
            "ctx_snapshot": {k: ctx.get(k) for k in
                             ("session_min", "time_et", "vwap", "ema9", "ema21", "ema135", "ema315",
                              "rsi", "day_range_pct", "rel_volume", "tape_score", "tape_grade",
                              "chip_verdict", "regime_live", "news") if ctx.get(k) is not None},
        }
        # a directional call must be settleable, or it is commentary and we discard it
        if direction != "NONE" and (not th["horizon_min"] or th["target_pct"] is None
                                    or th["invalidation_pct"] is None):
            return None
        try:
            import playbook as PB
            th["seq"] = PB.append("THESIS", signal_id=signal_id or f"th-{ticker}-{int(time.time())}",
                                  actor=actor, horizon=str(th["horizon_min"]), **th)
        except Exception:
            pass
        return th
    except Exception:
        return None


def _num(v):
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _str(v):
    return v if isinstance(v, str) else ("" if v is None else str(v))


def pending(limit=None, horizons=HORIZONS_MIN):   # limit MUST default None: see the warning below
    """Theses with at least one UNRESOLVED horizon — the queue Phase 3 works.

    Three corrections over the first version, each of which silently lost data:
      · indexed query, not tail() — a tail window let unrelated rows crowd out real theses and, once it
        overflowed, allowed the SAME thesis to be resolved twice into the report;
      · filtered to actor='llm' — server.py appends a THESIS for every ingested signal into the same
        kind namespace, so ordinary desk traffic was burying chip's theses;
      · pending on (signal_id, horizon) — keying on signal_id meant the first horizon to resolve blocked
        every longer one FOREVER, so 60/240/1950 could never populate. That is not just missing data, it
        is selection-biased data: a long horizon would only ever land after an outage.
    Returns [(event, [unresolved horizons])]."""
    try:
        import playbook as PB
        done = PB.resolved_pairs()
        out = []
        for e in PB.events_of_kind("THESIS", actor="llm", limit=limit):
            p = e.get("payload") or {}
            if not p.get("spot_at_proposal") or not p.get("proposed_utc"):
                continue
            todo = [h for h in horizons if (e.get("signal_id"), str(h)) not in done]
            if todo:
                out.append((e, todo))
        return out
    except Exception:
        return []


_INFLIGHT = threading.Semaphore(4)      # bounded: a slow API can never pile up threads on a 5s loop


def propose_async(ticker, ctx, *, actor="llm", signal_id=None):
    """Fire-and-forget proposal on a daemon thread.

    propose() makes a blocking API call with a 12s timeout. chip_desk.scan_loop runs every 5 SECONDS, so
    calling it inline would stall the entire scan — spot reads, cross detection, the book, everything.
    The desk's standing rule is that the LLM never sits on the hot path; this is that rule applied here."""
    if not _INFLIGHT.acquire(blocking=False):
        return False                     # already saturated -> skip this slot rather than queue behind it
    def _run():
        try:
            propose(ticker, ctx, actor=actor, signal_id=signal_id)
        except Exception:
            pass
        finally:
            _INFLIGHT.release()
    threading.Thread(target=_run, daemon=True).start()
    return True
