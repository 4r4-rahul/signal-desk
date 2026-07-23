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
DAILY_CAP = 60                              # hard money stop; a thesis costs ~$0.005
MIN_CONVICTION = 0.55                       # below this we record it but flag it as low-conviction

_STATE = {"day": None, "calls": 0}
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


def _budget_ok():
    with _LOCK:
        d = _today()
        if _STATE["day"] != d:
            _STATE["day"], _STATE["calls"] = d, 0
        if _STATE["calls"] >= DAILY_CAP:
            return False
        _STATE["calls"] += 1
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
        import llm_parse as LP
        if not LP.available() or not _budget_ok():
            return None
        raw = LP.call_tool(SYSTEM, THESIS_TOOL, _render(ticker, ctx), model=MODEL, max_tokens=MAX_TOKENS)
        if not raw or not isinstance(raw, dict):
            return None
    except Exception:
        return None

    spot = ctx.get("spot")
    if not spot:
        return None
    th = {
        "ticker": ticker, "spot_at_proposal": float(spot),
        "direction": raw.get("direction"), "horizon_min": raw.get("horizon_min"),
        "target_pct": raw.get("target_pct"), "invalidation_pct": raw.get("invalidation_pct"),
        "conviction": raw.get("conviction"), "regime": raw.get("regime"),
        "reasoning": (raw.get("reasoning") or "")[:600],
        "evidence": (raw.get("evidence") or [])[:6],
        "low_conviction": (raw.get("conviction") or 0) < MIN_CONVICTION,
        "proposed_utc": datetime.now(timezone.utc).isoformat(),
        "model": MODEL, "horizons_to_grade": list(HORIZONS_MIN),
    }
    # sanity: a directional call must actually be directional and settleable
    if th["direction"] not in ("LONG", "SHORT", "NONE"):
        return None
    if th["direction"] != "NONE":
        if not th["horizon_min"] or th["target_pct"] is None or th["invalidation_pct"] is None:
            return None
    try:
        import playbook as PB
        seq = PB.append("THESIS", signal_id=signal_id or f"th-{ticker}-{int(time.time())}",
                        actor=actor, horizon=str(th["horizon_min"]), **th)
        th["seq"] = seq
    except Exception:
        pass
    return th


def pending(limit=500):
    """THESIS events from the ledger that have no OUTCOME yet — the queue Phase 3 resolves."""
    try:
        import playbook as PB
        evs = PB.tail(limit, kind="THESIS")
        done = {e.get("signal_id") for e in PB.tail(limit * 4, kind="OUTCOME")}
        return [e for e in evs if e.get("signal_id") not in done
                and (e.get("payload") or {}).get("spot_at_proposal")]
    except Exception:
        return []
