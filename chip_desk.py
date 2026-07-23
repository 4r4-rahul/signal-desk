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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import deskpaths as _P
import market as MK
import chip_extension as X
import chip_probs as CP
import chip_strike as SK

ET = ZoneInfo("America/New_York")

# Phase 1 proves the loop on the two tightest-spread, cleanest-mechanics names. Phase 2 adds the rest.
TICKERS = ["SPY", "QQQ"]

# Per-ticker mechanics. *_pct are OPTION-premium %-moves (what _shadow_scaleout consumes), scaled to each
# ticker's ATM leverage — index ETFs (tight) get the smallest targets, single names ride wider. These are
# the fail-open DEFAULTS; chip_mechanics.json (nightly-calibrated) overrides them when present.
# DTE fields: dte_pref = preferred DTE, dte_max = cap, theta_pct_cap = roll out of 0DTE once daily decay
# exceeds this % of premium.
#
# GEOM g2 (dte_pref 0 -> 1 on the index ETFs + AAPL/NVDA). A fixed premium-% stop is NOT a fixed risk:
#     k = (s/100) · (0.3989/Δ) · √(T/w)        [IV cancels]
# so the stop distance measured in σ scales with √T. At 0DTE these same premium %s sit at 0.49-0.71σ —
# INSIDE the noise (QQQ 0.53σ, P(touch) 0.60). At 1DTE they are 1.08-1.57σ. The premium numbers were never
# wrong; the instrument was. Replaying the 7/21 QQQ 708C whipsaw tick-for-tick: 0DTE -1.00R (stopped),
# same 28/22/46/74 on 1DTE -0.04R (scratch). Hence: change the instrument, KEEP the number.
# Corollary the SNR derivation gives for free: leverage Ω cancels from signal and noise alike, so 0DTE's
# extra gamma buys no SNR — only theta drag, which is pure loss. 0DTE only wins on >1.53σ moves in 30 min.
DEFAULT_MECHANICS = {
    # spread_max 6 -> 3 on the index ETFs: SPY/QQQ ATM actually quote 0.5-0.6%, so a 6% cap never bound and
    # would have waved through a fill costing more than a quarter of T1. Single names left alone — there is
    # no evidence for a tighter number there, and inventing one is exactly what the SMEs warned against.
    # T1 RAISED TO 1.0x THE STOP (SPY 20->26, QQQ 22->32) and the QQQ stop widened 28->32.
    # The old t1/stop ratio (SPY 0.77, QQQ 0.79) banked half the position at well under 1R, truncating the
    # right tail that has to pay for the stops while the loss side still ran the full distance. Three
    # independent measurements agree on the SIGN:
    #   · SPY synthetic replay, 44 sessions, paired, session-block SE:  t1 -> 1.0x stop  +0.0065R +/- 0.0015
    #   · QQQ synthetic replay, 37 sessions, paired, day-block SE:      t1 -> 1.0x stop  +0.0034R +/- 0.0012
    #                                                                   t1 -> 1.4x stop  +0.0061R +/- 0.0024
    #   · REAL stored quotes (eval_harness, `path` ATM, paired, day-block SE):
    #         QQQ t1 22->32  +0.0304R +/- 0.0146 (z 2.09)     SPY t1 20->26  -0.0007R +/- 0.0050 (nil)
    # Monotone and same-signed on QQQ across all three; on SPY the real-quote sample is too thin to
    # confirm its own synthetic result and is consistent with zero. Shipped because the direction is
    # replicated and the downside is bounded, NOT because the magnitude is established.
    # QQQ stop 28->32: sizing arithmetic, not edge — at a $250 risk cap a 32% stop on a ~$4.8 ATM still
    # buys 1 contract, and it gives the exit somewhere to live. The QQQ review's z=13.7 "stop 35 vs 28"
    # is NOT evidence for this: that row moved t1 22->28 at the same time and double-counts the t1 effect.
    "SPY":  {"stop_pct": 26, "t1_pct": 26, "t2_pct": 55, "t3_pct": 88,  "spread_max": 3,  "strike_inc": 1.0, "regime_bias": "trend",    "dte_pref": 1, "dte_max": 2},
    "QQQ":  {"stop_pct": 32, "t1_pct": 32, "t2_pct": 60, "t3_pct": 95,  "spread_max": 3,  "strike_inc": 1.0, "regime_bias": "trend",    "dte_pref": 1, "dte_max": 2},
    "NVDA": {"stop_pct": 34, "t1_pct": 30, "t2_pct": 62, "t3_pct": 100, "spread_max": 10, "strike_inc": 2.5, "regime_bias": "momentum", "dte_pref": 1, "dte_max": 2},
    "TSLA": {"stop_pct": 38, "t1_pct": 34, "t2_pct": 70, "t3_pct": 115, "spread_max": 12, "strike_inc": 5.0, "regime_bias": "range",    "dte_pref": 1, "dte_max": 3},
    "AAPL": {"stop_pct": 30, "t1_pct": 24, "t2_pct": 50, "t3_pct": 80,  "spread_max": 8,  "strike_inc": 2.5, "regime_bias": "trend",    "dte_pref": 1, "dte_max": 2},
}
GEOM = "g2"                 # stamped on every emit so g1 (0DTE) and g2 (1DTE) samples never pool in edge stats

MIN_TAPE_MODERATE = 62      # chip's conviction bar. STILL COMPUTED AND LOGGED, NO LONGER A GATE — see
                            # TAPE_GATE_ENABLED. Kept as a constant because the score is still scored,
                            # displayed, stamped on every emit and available to any future review.
# ── THE TAPE GATE IS RETIRED (2026-07-22) ───────────────────────────────────────────────────────────
# It was never shown to select anything, and three independent measurements now agree:
#   · SPY synthetic replay, 44 sessions: the 62-68 band sits +0.002R +/- 0.028 vs null — literally nothing.
#   · QQQ synthetic replay, 37 sessions: the bar fires on 64.3% of minutes with 20-min forward drift
#     -0.07 bp +/- 1.07, and the volatility it fires into is 1.020x unconditional. It selects nothing.
#   · REAL stored quotes (eval_harness over `path` ATM ticks, day-block SE): the gated arm is WORSE than
#     the null on both tickers -- SPY -0.058R +/- 0.024, QQQ -0.032R +/- 0.025 at the DTE chip trades.
# So the gate is somewhere between worthless and mildly harmful, and it costs sample. Sample is the
# binding constraint on every question the desk actually wants answered (see the power note below), so
# a gate must EARN its selectivity. This one cannot.
#
# NOTE ON THE "30x MORE SAMPLE" CLAIM: retiring the 62 bar ALONE does not 30x anything -- the bar fires on
# ~64% of minutes. The binding constraint was the TRIGGER requirement below it (a fresh VWAP cross, or
# grade=='strong'), and _score_ex_vwap already documents that the cross and the 62 bar are anti-correlated
# by construction. The 30x needs BOTH retired, which is what this does.
# WHAT IS DELIBERATELY KEPT: the long-lookback EMA135/EMA315 trend gate in assess(). It is the only
# directional filter on this desk with a day-block bootstrap CI that excludes zero (+0.303R, 95% CI
# [+0.050, +0.539] over 220 triggers / 60 sessions). Retiring the unproven gates is not a reason to
# retire the proven one.
TAPE_GATE_ENABLED = False   # flip to True to restore the 62 bar + trigger requirement as hard gates
COOLDOWN_S = 900            # 15 min between chip entries on the same ticker (anti-churn)
# MAX_HOLD_S is a REAL TIME STOP, not just a book-slot release. It was 3600, and it only freed chip's
# in-memory slot: the SHADOW kept ticking until the contract EXPIRED, so a 1DTE scalp was scored over ~1.5
# sessions of theta. provider_edge was therefore measuring a strategy nobody would trade. Cutting it to
# 1200 was correct and STAYS.
#
# THE CURVE THAT JUSTIFIED IT WAS PARTLY WRONG, AND THE REASON MATTERS. The original monotone curve
#     hold 5m -0.050R  15m -0.115R  20m -0.140R  30m -0.159R  60m -0.219R  to-EOD -0.278R
# was reported as "1DTE" and as "pure theta". Re-measured on REAL stored quotes (eval_harness, `path`
# ATM ticks, day-block bootstrap SE), the slope is almost entirely a 0DTE effect:
#     SPY 0DTE     null  -0.090R (5m) -> -0.172R (20m) -> -0.414R (60m)   steeply monotone
#     SPY non-0DTE null  -0.027R (5m) -> +0.008R (20m) -> +0.082R (60m)   FLAT, if anything rising
# At the DTE chip actually trades the hold curve is flat, so MAX_HOLD_S is very nearly a free parameter
# anywhere in 20-60 min and the cost of getting it wrong is small. It is NOT free at 0DTE, which is one
# more reason dte_pref stays 1. Shortening to 5 min is the one move that is affirmatively bad: it cannot
# gain much (the curve is flat) and it raises the directional-accuracy hurdle (54.9% -> 56.9% on the SPY
# replay) because a fixed per-trade cost gets less time to be overcome.
# 20 min is kept because it is inside the flat region, it is where the accuracy hurdle is minimised, and
# changing it would cost a re-baseline of chip's sample for no measured gain.
# Emitted as chip_plan.max_hold_min so the server's shadow tracker actually closes the position
# (chip-only: no other provider sets it).
MAX_HOLD_S = 1200           # 20 min — time stop AND book-slot release
# ── POWER, STATED PLAINLY ────────────────────────────────────────────────────────────────────────────
# At ~0.55R per-trade dispersion and ~5 signals/day, detecting a +0.03R edge needs ~2,639 trades ~ 2.1
# YEARS, and that figure is itself an optimistic floor (it assumes iid trades; overlapping holds inside a
# session are not). +0.05R needs ~950 trades (~0.8y). What IS decidable on a season of data: large
# mechanical effects (0DTE vs 1DTE is -0.18R, z=-21 — decidable in weeks) and cost/geometry terms that
# can be measured PAIRED on identical entries. What is NOT decidable: any entry-signal edge smaller than
# ~+0.10R. Design accordingly — spend the sample on mechanics, not on hunting entry alpha.
LOOP_SLEEP = 5              # scan cadence — 5s is plenty for scalp entries; keeps the lap cheap

_MECH_CACHE = {"ts": 0.0, "data": None}
_NEWS_CACHE = {"ts": 0.0, "data": None}
_LAST_EMIT = {}             # ticker -> ts of last chip emit (cooldown)
_TAPE = {}                  # chip's OWN tape cache {ticker: (ts, ind)} — 1-min bars, decoupled from the
                            # shared 5-min human cache (which is keyed by ticker and would collide)


def _chip_indicators(bars):
    """EMA9/EMA21/RSI/Bollinger over the full recent 1-min series (needs prior-day history so chip can read
    tape from the OPEN, not ~20 min in), but VWAP + price anchored to TODAY's session only — a multi-day
    VWAP would be wrong. This is the fix for the open cold-start."""
    if not bars or len(bars) < 21:
        return None
    ind = MK.indicators(bars)
    if not ind:
        return None
    tb = None                       # today's bars; stays None if the block below fails -> fall back to `bars`
    try:
        import numpy as np
        today = bars[-1].date.date()
        tb = [b for b in bars if getattr(b, "date", None) is not None and b.date.date() == today]
        if tb:
            h = np.array([b.high for b in tb], float); l = np.array([b.low for b in tb], float)
            c = np.array([b.close for b in tb], float); v = np.array([b.volume for b in tb], float)
            tp = (h + l + c) / 3
            ind["vwap"] = round(float((tp * v).sum() / v.sum()), 2) if v.sum() > 0 else round(float(c[-1]), 2)
            ind["price"] = round(float(c[-1]), 2)
    except Exception:
        pass
    # Raw closes for the EXTENSION guard. It needs a window (100 min) LONGER than the move being judged,
    # which is exactly what BB(20)-on-1-min cannot give — 20 bars re-centre on the move you are chasing.
    # Bounded slice, so this can't grow the cached dict without limit.
    # SESSION-FILTERED, like the VWAP above. Slicing the raw 2-day pull meant that for the first ~100
    # minutes of every session the band straddled the overnight gap: sd was then driven by the gap, not by
    # intraday range. Measured on 60 sessions, 39% of triggers had a straddling window and the penalty
    # disagreed with a session-only window on 69% of pre-11:10 cases. Under MIN_BARS the guard degrades
    # honestly (penalty 0) instead of silently reading a bimodal mixture.
    try:
        ind["closes"] = [float(b.close) for b in (tb or bars)][-(X.DEFAULT_HORIZON_MIN + 20):]
    except Exception:
        ind["closes"] = []
    return ind


def _tape(conn, tkr, ttl=45):
    """Chip's intraday tape on 1-MINUTE bars (2-day pull for the momentum windows, VWAP anchored to today).
    1-min suits scalping better than 5-min, and the prior-day history means chip reads tape from the open.
    Cached per ticker, ≤1 pull/45s — decoupled from the shared 5-min human cache."""
    import time as _t
    c = _TAPE.get(tkr)
    if not c or _t.time() - c[0] > ttl:
        try:
            # 5 D, MEASURED not assumed. EMA315 needs 945 bars (3x span, so the truncated seed residual is
            # 5.9e-4 instead of 7.1e-3). IBKR's `duration` counts CALENDAR days and returns only RTH bars,
            # so "3 D" yields ~788 -- under the threshold -- and ema315 silently stayed None, leaving the
            # trend gate inert from the moment it shipped. It fails open, so nothing broke loudly; chip
            # simply never applied the one filter that measured +0.303R. "5 D" yields ~1568. Cost is
            # unchanged in practice: get_bars is ~380ms of NETWORK once per 45s cache, and indicators over
            # the whole series run in ~1ms.
            ind = _chip_indicators(MK.get_bars(conn, tkr, duration="5 D", size="1 min")) or {}
        except Exception:
            ind = {}
        _TAPE[tkr] = (_t.time(), ind)
    return _TAPE[tkr][1]

# ── OBSERVER STATE (bounded) — so the UI can show exactly what chip sees + why it acts/waits ──
CROSS_WINDOW_S = 180        # a VWAP reclaim/breakdown stays a VALID trigger for ~3 min. Without this the
                            # cross (a one-lap EVENT) had to coincide exactly with tape≥62 (a STATE) on the
                            # same 5s lap — miss by one lap and the setup was lost. That conjunction is why
                            # chip produced zero samples for two sessions.
_LAST_CROSS = {}            # ticker -> (ts, 'up'|'down')
STATUS = {}                 # ticker -> live status dict (rebuilt each lap; ≤ len(TICKERS))
ACTIVITY = []               # rolling last-N chip events (emits/notes) for the observer log
SCAN = {"ts": 0.0, "session": "closed"}
_PREV_SPOT = {}


def _srv():
    """The RUNNING server module — NEVER `import server`.

    The desk runs as `python server.py`, so the live module is __main__. A plain `import server` builds a
    SECOND copy of server.py with its OWN SIGNALS list: chip's cards get appended to that orphan copy,
    logged as CARD, and then wiped when the real server saves over _signals.json. That silently swallowed
    every chip scalp. Resolve to __main__ when it's the server; fall back to a normal import for tests."""
    import sys
    m = sys.modules.get("__main__")
    if m is not None and hasattr(m, "ingest") and hasattr(m, "SIGNALS"):
        return m
    import server as s
    return s


def _activity(msg):
    ACTIVITY.append({"t": _now_et().strftime("%H:%M:%S"), "msg": msg})
    del ACTIVITY[:-15]      # keep the last 15 only


def snapshot():
    """Everything the observer UI needs: per-ticker-per-DIRECTION status + a recent-activity log. Bounded
    (≤ 2×len(TICKERS)). Keys are 'TKR|C' / 'TKR|P' so both scalp directions surface independently."""
    keys = [f"{t}|{s}" for t in TICKERS for s in ("C", "P")]
    return {"tickers": [STATUS[k] for k in keys if k in STATUS],
            "watching": list(TICKERS),
            "session": SCAN["session"],
            "last_scan": SCAN["ts"] or None,
            "activity": ACTIVITY[-15:]}


# ─────────────────────────── session / time ───────────────────────────
def _now_et():
    return datetime.now(ET)


def _session_min(now):
    """Minutes since the 9:30 ET open, or None when the market is closed (weekend / outside 9:30–16:00)."""
    if now.weekday() >= 5:
        return None
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 30)
    return mins if 0 <= mins <= 390 else None


def _mins_to_close(now, dte):
    """TRADING minutes of decay left until the contract expires. The `+ 390*dte` term is the whole point:
    without it a 1DTE was told it had a few hours to live when it has that plus a full 390-min session,
    which understates its remaining extrinsic and makes every theta number wrong by a whole day."""
    to_bell = (16 * 60) - (now.hour * 60 + now.minute)
    return max(0, to_bell) + 390 * int(dte or 0)


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
def _score_ex_vwap(st):
    """market.tape_strength's score with the VWAP-DISTANCE term removed and the surviving weights
    renormalised. Returns 0-100, or None if the components aren't readable.

    Why this exists. tape_strength gives price-vs-VWAP a 0.35 weight (the largest of the four) scaled so
    that full strength is 0.6% beyond VWAP. At a VWAP CROSS price is AT VWAP by construction, so that
    term is ~0 — the score is mechanically suppressed at exactly the instant chip's cross trigger wants
    to fire. MEASURED over 20 sessions / 30,608 ticker-side-minutes of SPY+QQQ 1-min tape:

        inside a fresh cross window   |price-VWAP| 0.069%  ->  VWAP term 0.12 of 1.00   score>=62: 14.3%
        outside one                   |price-VWAP| 0.297%  ->  VWAP term 0.46 of 1.00   score>=62: 29.7%

    So the cross trigger and the 62 bar are anti-correlated BY CONSTRUCTION, and requiring both is close
    to requiring a contradiction. That is why all six of chip's real emits fired on 'strong momentum' and
    not one on a cross: the cross detector was never broken, it was unreachable.

    This is a DE-BIAS, not a loosening. The surviving evidence (EMA separation, RSI, Bollinger room) is
    held to the SAME 62 bar; only the term the cross itself zeroes is dropped. It is used solely when a
    fresh favourable cross is live, and only when it RAISES the reading, so it can never block a setup
    that already qualified."""
    comps = (st or {}).get("components") or []
    w = {"EMA": 0.25, "RSI": 0.25, "room": 0.15}
    tot = sum(w[c["k"]] for c in comps if c["k"] in w)
    if tot <= 0:
        return None
    raw = sum(w[c["k"]] * c["s"] for c in comps if c["k"] in w) / tot
    return round((raw + 1) / 2 * 100)


def assess(tkr, ind, mech, session_min, side, crossed_up=False, crossed_down=False):
    """Assess ONE direction (`side` = 'C' or 'P') for an ATM scalp — ALWAYS returns a verdict dict so the
    observer can show WHY chip acts or waits. The two directions are anti-correlated (a strong call ⇒ an
    against put), so the caller evaluates BOTH and the stronger one is chip's 'primary' pick; the other is
    a 'counter' shadow (tagged, kept out of the real edge). TAKE needs: grade not against/weak; score ≥
    MIN_TAPE_MODERATE; AND a trigger — a fresh VWAP reclaim (C, crossed_up) / breakdown (P, crossed_down)
    from the FRESH per-lap spot, or strong momentum. Midday-strong gate dropped (shadow data-gen); every
    TAKE tagged live_grade (live=would pass strict / explore=loosened only). Pure + deterministic."""
    regime = mech.get("regime_bias", "trend")
    if not ind or ind.get("rsi") is None or not ind.get("vwap") or not ind.get("price"):
        return {"verdict": "WAIT", "reason": "no tape data yet", "score": None, "grade": "no-data",
                "cp": side, "regime": regime}
    st = MK.tape_strength(side, ind)
    base = {"cp": side, "regime": regime, "score": st["score"], "grade": st["grade"]}
    if st["score"] is None:
        return {**base, "verdict": "WAIT", "reason": "no tape read"}
    # A fresh favourable cross is judged on the tape MINUS the term the cross itself zeroes (see
    # _score_ex_vwap). Same 62 bar, applied to the evidence that is still readable at a cross.
    cross_fav = bool((side == "C" and crossed_up) or (side == "P" and crossed_down))
    bar_score, bar_basis = st["score"], "tape"
    if cross_fav:
        sx = _score_ex_vwap(st)
        if sx is not None and sx > st["score"]:
            bar_score, bar_basis = sx, "tape ex-VWAP (at a cross, price IS VWAP)"
            base = {**base, "bar_score": sx, "bar_basis": bar_basis}
    # The 62 bar is RETIRED as a gate (see TAPE_GATE_ENABLED). The score is still computed above and
    # still travels on every verdict in `base`, so it is logged, displayed and available to any future
    # review — it simply no longer decides. Flipping TAPE_GATE_ENABLED back to True restores the old
    # behaviour exactly.
    if TAPE_GATE_ENABLED:
        if st["grade"] == "against" and bar_score < MIN_TAPE_MODERATE:
            return {**base, "verdict": "WAIT", "reason": f"tape against ({st['score']}) — stand down"}
        if bar_score < MIN_TAPE_MODERATE:
            return {**base, "verdict": "WAIT", "reason": f"tape {st['score']} ({st['grade']}) — below the {MIN_TAPE_MODERATE} bar"}
    # LONG-LOOKBACK TREND GATE. Binary, sign-only, sigma-FREE — it reads an EMA ordering, not a magnitude,
    # so no volatility input can flip it. Measured over 220 triggers / 60 sessions: trades agreeing with
    # this trend returned +0.046R, trades fighting it -0.261R, delta +0.303R (day-block bootstrap 95% CI
    # [+0.050, +0.539], P(delta>0) = 0.991). Deliberately NOT a score penalty — the score is not predictive
    # enough to tune on (raising the bar 74->76 gives z=+0.38; dwell/hysteresis gives nothing), so a
    # penalty would just reintroduce a surface to overfit. Fail-open when the EMAs are absent.
    e135, e315 = ind.get("ema135"), ind.get("ema315")
    if e135 is not None and e315 is not None:
        if (e135 > e315) != (side == "C"):
            return {**base, "verdict": "WAIT", "htf": {"ema135": e135, "ema315": e315, "agrees": False},
                    "reason": f"against the ~5h trend (EMA135 {e135} {'<' if e135 < e315 else '>'} EMA315 {e315})"}
        base = {**base, "htf": {"ema135": e135, "ema315": e315, "agrees": True}}
    trig = None
    if side == "C" and crossed_up:
        trig = "VWAP reclaim" + (" (ex-VWAP tape " + str(bar_score) + ")" if bar_basis != "tape" else "")
    elif side == "P" and crossed_down:
        trig = "VWAP breakdown" + (" (ex-VWAP tape " + str(bar_score) + ")" if bar_basis != "tape" else "")
    if not trig and st["grade"] == "strong":
        trig = "strong momentum"
    if not trig and TAPE_GATE_ENABLED:
        return {**base, "verdict": "WAIT",
                "reason": f"tape {st['score']} ({st['grade']}), {'call' if side=='C' else 'put'} side — waiting for a trigger"}
    if not trig:
        # Trigger requirement retired with the tape bar. The HTF trend gate above has already agreed with
        # this side, which is now the whole directional case — stated honestly rather than dressed up as
        # a momentum trigger that did not fire.
        trig = "HTF trend (no tape trigger — gate retired)"
    # would this have passed the STRICT bar? (strict only demanded 'strong' DURING midday 45–300 min)
    strict_ok = st["grade"] == "strong" or not (session_min is not None and 45 < session_min < 300)
    return {**base, "verdict": "TAKE", "reason": trig,
            "live_grade": "live" if strict_ok else "explore", "strict_take": bool(strict_ok)}


ARM_MARGIN = 6                  # build the ladder one band EARLY so its quote streams are warm at trigger
_LADDER_TTL = 20                # the DECISION must be fresh at trigger time; the DISPLAY tolerates 20s
_LADDER = {}                    # f"{tkr}|{side}" -> (ts, result). Bounded by 2*len(TICKERS).


def pick_strike(conn, tkr, side, spot, mech, dte_tuple, now, score, crossed):
    """Choose the strike by VETO, not by optimiser (see chip_strike's module docstring: under a %-of-premium
    stop the ladder's expectancy is flat, so ranking on EV would fit noise). Quotes 2N+1 rungs ONLY for an
    ARMED side, so the steady-state IBKR line cost is ~5 extra streams against reap_subs' cap of 80.

    Returns (chosen_quote, chosen_strike, ladder_result) or (None, atm_strike, None) when not armed — in
    which case the caller keeps its existing single-ATM behaviour."""
    inc = mech.get("strike_inc", 1.0)
    atm = round(spot / inc) * inc
    armed = bool(crossed or (score is not None and score >= MIN_TAPE_MODERATE - ARM_MARGIN))
    if not armed:
        return None, atm, None
    key = f"{tkr}|{side}"
    c = _LADDER.get(key)
    if c and time.time() - c[0] < _LADDER_TTL and c[1].get("spot_ref") == round(spot, 2):
        return c[1].get("_q"), c[1]["chosen"]["strike"] if c[1].get("chosen") else atm, c[1]
    dte, expiry = dte_tuple[0], dte_tuple[1]
    mc = _mins_to_close(now, dte)
    quotes, qmap = [], {}
    for k in SK.ladder_strikes(spot, inc):
        ks = str(int(k)) if float(k).is_integer() else str(k)
        try:
            q = MK.option_quote(tkr, ks, side, expiry=expiry, ib=conn)
        except Exception:
            continue
        if not q or q.get("error") or q.get("mid") is None:
            continue
        # the theta fuse is a veto input, so it must come from the same engine the card displays
        fuse_min = fuse_state = None
        try:
            pr = CP.touch_probs(spot=spot, strike=k, cp=side, mark=q.get("mid"), entry=q.get("ask") or q.get("mid"),
                                delta=q.get("delta"), gamma=q.get("gamma"), iv=q.get("iv"),
                                t1_pct=mech.get("t1_pct"), t2_pct=mech.get("t2_pct"), t3_pct=mech.get("t3_pct"),
                                cur_stop_pct=-abs(mech.get("stop_pct") or 0), stop_pct=mech.get("stop_pct"),
                                mins_to_close=mc, dte=dte, ticker=tkr, hold_min=MAX_HOLD_S // 60) or {}
            fuse_min, fuse_state = pr.get("stop_theta_min"), pr.get("fuse_state")
        except Exception:
            pass
        row = dict(q); row.update(strike=k, fuse_min=fuse_min, fuse_state=fuse_state)
        quotes.append(row); qmap[k] = q
    res = SK.select(quotes=quotes, spot=spot, mech=mech, ticker=tkr,
                    hold_min=MAX_HOLD_S // 60, equity=_equity(), max_risk_pct=1.0)
    res["spot_ref"] = round(spot, 2)
    chosen = res.get("chosen")
    # The 1% cap is now applied INSIDE select(), as a feasibility filter over the ladder rather than as a
    # post-hoc test on the single winner (see chip_strike.select: ranking first and sizing second refused
    # 100% of QQQ 1DTE, because the lowest-leverage rung is by construction the most expensive one).
    # This stays as a belt-and-braces assertion: nothing chosen may ever breach the cap.
    if chosen:
        qty, risk1 = SK.size_for(chosen.get("premium"), mech.get("stop_pct"), _equity(), 1.0)
        res["qty"], res["risk_per_contract"] = qty, risk1
        if qty < 1:                         # unreachable via select(); kept so a future edit cannot leak
            res["degraded"] = True
            res["note"] = f"${risk1:.0f} risk/contract over the 1% cap — not tradeable at this equity"
            res["_q"] = None
            _LADDER[key] = (time.time(), res)
            return None, atm, res           # fail CLOSED: no quote -> caller emits nothing
    if not chosen:                          # no rung cleared the vetoes -> refuse, don't fall back to ATM
        res["_q"] = None
        _LADDER[key] = (time.time(), res)
        return None, atm, res
    ck = chosen["strike"]
    res["_q"] = qmap.get(ck)
    _LADDER[key] = (time.time(), res)
    return res["_q"], ck, res


def _equity():
    """chip sizes against its OWN PAPER ACCOUNT, never the live tastytrade sleeve.

    chip is shadow-only, so borrowing the real balance was wrong in both directions: it let the sleeve's
    drawdown silently shrink chip's sample (a $96 cap refused 100% of QQQ for two days — a measurement
    outage caused by an account chip does not trade), and it invited chip's paper results to be read as
    if they shared a book with real money. chip_paper rolls its own equity from chip's stored shadow
    outcomes, so the account compounds and decays on its own record."""
    try:
        import chip_paper
        return float(chip_paper.equity())
    except Exception:
        return None


def apply_extension(a, ind, mech, spot, pq, side):
    """OBSERVE-ONLY. Computes the runway read and attaches it to the card and the emit record; it does NOT
    gate entries.

    It WAS a gate for a few hours on 2026-07-21 and the evidence killed it. Replayed over 220 triggers on
    60 sessions: trades it blocked returned −0.037R, trades it passed returned −0.053R — delta −0.016,
    z = −0.12. It blocked 66% of trend-agreeing trades and 75% of trend-fighting ones, a 9pp gap
    indistinguishable from noise. It is not a filter; it is a random 69% decimation of the sample, and it
    tripled the sessions needed to learn anything (24 -> 71).

    The ~67% veto rate on chip's own entries — which the first version cited as evidence the guard was
    finding something real — is just the base rate of the statistic, not discrimination.

    Kept computed (not deleted) because `ext` is logged on every emit: once ~150 chip signals carry it,
    whether runway predicts anything becomes a question data can answer instead of argument."""
    closes = (ind or {}).get("closes") or []
    mid = (pq or {}).get("mid")
    delta = (pq or {}).get("delta")
    req = None
    if mid and delta and abs(delta) > 1e-6 and mech.get("t1_pct"):
        req = (mech["t1_pct"] / 100.0 * mid) / abs(delta)
    ext = X.assess(spot=spot, closes=closes, req_pts=req, side=side)
    a = {**a, "ext": ext}
    pen = ext.get("penalty") or 0
    if a.get("score") is not None and pen:
        a["score_eff"] = a["score"] - pen          # recorded for later analysis; NOT acted on
    return a


def decide(tkr, ind, mech, session_min, side, crossed_up=False, crossed_down=False):
    """Thin wrapper: the entry spec when assess() says TAKE, else None (kept for existing call sites/tests)."""
    a = assess(tkr, ind, mech, session_min, side, crossed_up, crossed_down)
    return a if a["verdict"] == "TAKE" else None


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
        server = _srv()
        sig = next((s for s in server.SIGNALS if s.get("id") == b["sid"]), None)
    except Exception:
        return
    if not sig:
        book.pop(tkr, None)
        return
    state = (sig.get("shadow") or {}).get("state")
    if state and state != "live":
        book.pop(tkr, None)


def _rebuild_book(book):
    """A restart wipes chip's in-memory book, which ORPHANS any still-open scalp: the card loses its live
    tape read (thesis goes 'no live read') and chip would happily double-enter the same ticker+side.
    Rebuild from the live chip shadows already in SIGNALS so a restart is transparent."""
    try:
        server = _srv()
        for s in server.SIGNALS:
            if s.get("provider") != "chip":
                continue
            sh, sg = s.get("shadow") or {}, s.get("sig") or {}
            if sh.get("state") != "live":
                continue
            tkr, cp = sg.get("ticker"), sg.get("type")
            if not tkr or not cp or tkr not in TICKERS:
                continue
            opened = time.time()
            try:
                opened = datetime.fromisoformat(sh["start_utc"]).timestamp()
            except Exception:
                pass
            key = f"{tkr}|{cp}"
            book[key] = {"sid": s["id"], "opened": opened, "cp": cp}            # newest wins
            # restore the COOLDOWN clock too — rebuilding only `book` let a restart bypass COOLDOWN_S
            # entirely, so chip could re-enter the same ticker+side seconds after coming back up.
            _LAST_EMIT[key] = max(_LAST_EMIT.get(key, 0), opened)
        if book:
            print(f"🤖 chip desk: adopted {len(book)} open scalp(s) after restart: {sorted(book)}", flush=True)
    except Exception as e:
        print(f"🤖 chip desk: book rebuild skipped ({e})", flush=True)


def _cooling(tkr, now):
    return (now.timestamp() - _LAST_EMIT.get(tkr, 0)) < COOLDOWN_S


# ─────────────────────────── emit (through the normal ingest path) ───────────────────────────
def _date_label(ymd):
    """A round-trip-SAFE label for a snapped expiry: 'MM/DD' (e.g. '07/22'). The slash disambiguates from
    the premium digits (a 'Jul 22' after '@ 1.20' collides — the '20' misparses as the day). And NEVER emit
    'NDTE' for N>1 — server's _resolve_expiry turns that into a raw calendar offset, not the LISTED date."""
    return f"{int(ymd[4:6]):02d}/{int(ymd[6:8]):02d}"


def dte_choice(ticker, mech, now, expiries):
    """Mechanics-aware DTE selector. SINGLE SOURCE shared by build_chip_read (preview) AND _emit (fill).
    Pure: `expiries` is a pre-fetched, cached, sorted list of YYYYMMDD (from market.list_expiries).
    Returns (dte, expiry_yyyymmdd, label, why). dte_pref → time-of-day bump (past 1pm, the real 0DTE theta
    cliff) → dte_max cap → SNAP to nearest LISTED expiry ≥ target. Fail-open to the clock rule if empty.
    NOTE: no θ%/premium gate — a 0DTE's θ is ~100%/day by construction, so that gate trips always and would
    kill 0DTE entirely; the late-day cliff (the real risk) is the time-of-day bump."""
    today = now.strftime("%Y%m%d")
    if not expiries:                                        # enumeration failed -> legacy clock rule
        if now.hour < 13:
            return (0, today, "0DTE", "expiry list unavailable → clock rule (0DTE)")
        return (1, (now + timedelta(days=1)).strftime("%Y%m%d"), "1DTE", "expiry list unavailable → clock rule (1DTE)")
    pref = mech.get("dte_pref", 0)
    dmax = mech.get("dte_max", 1)
    target, notes = pref, []
    if pref == 0 and now.hour >= 13:
        target = 1; notes.append("past 13:00 ET — dodging the 0DTE theta cliff into the close")
    target = min(target, dmax)
    target_ymd = (now + timedelta(days=target)).strftime("%Y%m%d")
    snapped = next((e for e in expiries if e >= target_ymd), expiries[-1])   # nearest listed ≥ target, else last
    real_dte = (datetime.strptime(snapped, "%Y%m%d").date() - now.date()).days
    if snapped == today:
        label = "0DTE"
    elif real_dte == 1:
        label = "1DTE"
    else:
        label = _date_label(snapped)                        # round-trip-safe explicit date for any snap >1
    if real_dte != target and target < dmax + 1:
        notes.append(f"no {target}DTE listed → nearest {label} ({real_dte}DTE)")
    why = (f"{label} — " + ("; ".join(notes) if notes else
           ("daily expiry, max gamma" if real_dte == 0 else "per-ticker DTE bias")))
    return (real_dte, snapped, label, why)


_TAPE_WEIGHTS = {"VWAP": 0.35, "EMA": 0.25, "RSI": 0.25, "room": 0.15}   # market.tape_strength blend


def build_chip_read(ind, mech, spot, pq, a, now, smin, strike, dte_tuple):
    """chip's REASONING block for a watch card: strike + DTE verdicts (why optimal), tape decomposition,
    fill quality, distance-to-trigger, and the would-be plan. Pure — takes the already-computed
    ind/quote/assess AND the DTE decision (from dte_choice) so it's unit-testable and adds no I/O."""
    if not (spot and a and a.get("cp") and pq and not pq.get("error") and pq.get("mid")):
        return {"available": False, "reason": (a or {}).get("reason") or "no quote yet"}
    cp = a["cp"]
    smax = mech.get("spread_max", 8)
    delta, spread = pq.get("delta"), pq.get("spread_pct")
    ad = abs(delta) if delta is not None else None
    mid, ask = pq.get("mid"), pq.get("ask")
    live_px = ask or mid                                # chip fills at the market ask (crosses the spread)
    # ── strike verdict ──
    if spread is not None and spread > smax:
        sv, swhy = "wide", f"spread {spread}%>{smax}% cap — fill cost eats the edge"
    elif ad is None:
        sv, swhy = "acceptable", "ATM strike — delta read pending"
    elif 0.40 <= ad <= 0.60:
        sv, swhy = "optimal", f"Δ{ad:.2f} ATM — max gamma-per-$ for a scalp, spread {spread}%≤{smax}%"
    elif 0.30 <= ad <= 0.70:
        sv, swhy = "acceptable", f"Δ{ad:.2f} near-ATM — fill cost fine (spread {spread}%)"
    elif ad < 0.30:
        sv, swhy = "wide", f"Δ{ad:.2f} — too far OTM, theta bleeds the pop (lotto)"
    else:
        sv, swhy = "wide", f"Δ{ad:.2f} — deep ITM ties up capital for little gamma"
    strike_b = {"strike": strike, "delta": delta,
                "moneyness_pct": round((spot - strike) / strike * 100, 3) if strike else None,
                "spread_pct": spread, "verdict": sv, "why": swhy}
    # ── DTE verdict (mechanics-aware — dte_tuple decided by dte_choice against the LISTED expiries) ──
    dte, expiry, dte_label, dwhy = dte_tuple
    theta = pq.get("theta")
    theta_pct = round(abs(theta) / mid * 100) if (theta and mid) else None   # daily decay as % of premium
    if dte == 0 and theta_pct is not None:                                   # honest DISPLAY note (not a gate)
        dwhy = dwhy + f"; θ≈{theta_pct}%/day — scalp it quick, bleeds into the close"
    dte_b = {"dte": dte, "expiry": expiry, "label": dte_label, "theta": theta, "theta_pct": theta_pct,
             "verdict": dte_label, "why": dwhy, "dte_pref": mech.get("dte_pref", 0)}
    # ── tape decomposition (BOTH sides scored so the marginality is visible; favor-consistent plain text) ──
    st = MK.tape_strength(cp, ind)
    st_other = MK.tape_strength("P" if cp == "C" else "C", ind)
    cp_other = "P" if cp == "C" else "C"
    side_word = "calls" if cp == "C" else "puts"

    def _plain(k, sc):
        if k == "VWAP":
            return "price on the favorable side of VWAP" if sc > 0.05 else \
                   "price on the WRONG side of VWAP" if sc < -0.05 else "price right at VWAP"
        if k == "EMA":
            return f"EMA trend supports the {side_word}" if sc > 0.05 else \
                   "EMA trend leans AGAINST" if sc < -0.05 else "EMAs flat — no trend"
        if k == "RSI":
            r = ind.get("rsi")
            tag = " momentum with us" if sc > 0.05 else " momentum against" if sc < -0.05 else " neutral"
            return (f"RSI {round(r)} —{tag}") if r is not None else "RSI n/a"
        if k == "room":
            way = "run (up)" if cp == "C" else "fall (down)"
            return f"room to {way}" if sc > 0.05 else "pinned at the band — little room" if sc < -0.05 else "mid-channel"
        return ""
    factors = [{"factor": c["k"], "score": c["s"], "weight": _TAPE_WEIGHTS.get(c["k"], 0),
                "contribution": round(c["s"] * _TAPE_WEIGHTS.get(c["k"], 0), 3), "plain": _plain(c["k"], c["s"])}
               for c in (st.get("components") or [])]
    aligned = sum(1 for f in factors if f["score"] > 0.05)
    tape_b = {"score": st.get("score"), "grade": st.get("grade"), "icon": st.get("icon"),
              "bars": st.get("bars"), "factors": factors, "side": cp,
              "cp_other": cp_other, "score_other": st_other.get("score"),
              "summary": f"{aligned}/4 factors aligned — {cp} {st.get('score')} vs {cp_other} {st_other.get('score')}"}
    # ── FILL QUALITY (chip crosses the spread at the market ask for immediacy — this is fill quality,
    #    NOT a price to wait for; the tape TRIGGER below is the real go/no-go). ──
    fill_ok = (spread is not None and spread <= smax)
    fquality = "tight" if (spread is not None and spread <= smax * 0.5) else "ok" if fill_ok else "wide"
    fill_b = {"mid": mid, "ask": ask, "live_px": live_px, "spread_pct": spread, "spread_max": smax,
              "quality": fquality, "ok": bool(fill_ok)}
    # ── distance-to-trigger (mirrors assess branches) ──
    px, vwap, score = ind.get("price"), ind.get("vwap"), a.get("score")
    if a.get("verdict") == "TAKE":
        trig = {"trigger_type": "ready", "needs": f"trigger live: {a.get('reason')}", "gap": 0, "ready": True}
    elif a.get("grade") == "against":
        trig = {"trigger_type": "none", "needs": "tape against — stand down", "gap": None, "ready": False}
    elif score is not None and score < MIN_TAPE_MODERATE:
        trig = {"trigger_type": "tape", "needs": f"tape {score} < {MIN_TAPE_MODERATE} bar",
                "gap": MIN_TAPE_MODERATE - score, "ready": False}
    elif px and vwap:
        if cp == "C":
            trig = {"trigger_type": "vwap_reclaim", "needs": f"reclaim VWAP {vwap} (now {px})",
                    "gap": round(vwap - px, 2), "ready": False}
        else:
            trig = {"trigger_type": "vwap_breakdown", "needs": f"lose VWAP {vwap} (now {px})",
                    "gap": round(px - vwap, 2), "ready": False}
    else:
        trig = {"trigger_type": "tape", "needs": a.get("reason") or "waiting", "gap": None, "ready": False}
    # ── would-be plan preview (off the ask chip would pay; == the managed plan) ──
    stop, t1, t2, t3 = mech.get("stop_pct"), mech.get("t1_pct"), mech.get("t2_pct"), mech.get("t3_pct")
    e = live_px
    pxat = lambda p: round(e * (1 + p / 100.0), 2) if (e and p is not None) else None
    plan_b = {"entry": e,
              "stop": {"pct": stop, "px": round(e * (1 - stop / 100.0), 2) if (e and stop) else None, "r": -1.0},
              "t1": {"pct": t1, "px": pxat(t1), "r": round(t1 / stop, 2) if (t1 and stop) else None},
              "t2": {"pct": t2, "px": pxat(t2), "r": round(t2 / stop, 2) if (t2 and stop) else None},
              "t3": {"pct": t3, "px": pxat(t3), "r": round(t3 / stop, 2) if (t3 and stop) else None}}
    # ── live regime (honest read) so it doesn't contradict a "midday chop" trigger with a static "trend" ──
    e9, e21, px2 = ind.get("ema9"), ind.get("ema21"), ind.get("price")
    sep = abs(e9 - e21) / px2 * 100 if (e9 and e21 and px2) else 0
    if smin is not None and 45 < smin < 300 and sep < 0.05:
        regime_live = "midday chop"
    elif sep < 0.03:
        regime_live = "range"
    else:
        regime_live = "trend"
    # combined GO signal: only truly "ready" when the tape trigger fires AND the fill is clean
    ready = bool(trig.get("ready") and fill_b["ok"])
    # ── EXTENSION: is there room left for the move T1 needs? (attached by apply_extension before this call)
    ext_b = a.get("ext") or {"verdict": "unknown", "penalty": 0, "degraded": True}
    # ── PROBABILITIES: P(T1/T2/T3), P(stop), the theta fuse, and the move T1 actually requires. Display-only
    # by chip_probs' own honesty contract; the ACTION stays on the σ-free terms (t1_first + the fuse).
    probs_b = None
    try:
        mc = _mins_to_close(now, dte)
        if e and mc:
            probs_b = CP.touch_probs(spot=spot, strike=strike, cp=cp, mark=mid, entry=e,
                                     delta=pq.get("delta"), gamma=pq.get("gamma"), iv=pq.get("iv"),
                                     t1_pct=t1, t2_pct=t2, t3_pct=t3, cur_stop_pct=-abs(stop or 0),
                                     stop_pct=stop, mins_to_close=mc, dte=dte, ticker=None,
                                     hold_min=MAX_HOLD_S // 60)
    except Exception:
        probs_b = None
    return {"available": True, "strike": strike_b, "dte": dte_b, "tape": tape_b,
            "fill": fill_b, "trigger": trig, "plan": plan_b, "regime_live": regime_live, "ready": ready,
            "extension": ext_b, "probs": probs_b, "ladder": a.get("ladder")}


def build_live_read(ind, spot, side, sid, ts):
    """Live tape read for a ticker chip is CURRENTLY HOLDING — the in-scalp twin of build_chip_read, so the
    live card can answer "is the entry thesis still true?". PURE: consumes the already-cached 1-min `ind`
    and the warm per-lap `spot`, so it adds ZERO I/O. `sid` stamps the owning signal so the server can
    never cross-join this read onto a different scalp on the same ticker+side."""
    if not ind or not ind.get("price") or not ind.get("vwap"):
        return None
    st = MK.tape_strength(side, ind)                       # SAME function used at entry -> comparable
    is_c = side == "C"
    px, vwap = ind["price"], ind["vwap"]
    e9, e21 = ind.get("ema9"), ind.get("ema21")
    return {"sid": sid, "ts": ts, "side": side, "spot": spot,
            "score": st.get("score"), "grade": st.get("grade"),
            "price": px, "vwap": vwap,
            "vfav": round((px - vwap) / vwap * 100 * (1 if is_c else -1), 3) if vwap else None,
            "ema_side": (("with" if (e9 > e21) == is_c else "against") if (e9 and e21) else None),
            "rsi": ind.get("rsi"),
            "factors": [{"factor": c["k"], "score": c["s"]} for c in (st.get("components") or [])]}


def _rungs_snapshot(chip_read):
    """The ladder as it stood at the moment of the emit, minus the rung we took. Bounded and flat, so it
    survives into the archive and the DB row without bloating either. Every field here is already computed
    — this only stops it being thrown away."""
    try:
        lad = (chip_read or {}).get("ladder") or {}
        out = []
        for r in (lad.get("rungs") or []):
            if r.get("chosen"):
                continue
            out.append({"strike": r.get("strike"), "delta": r.get("delta"), "premium": r.get("premium"),
                        "spread_pct": r.get("spread_pct"), "lam": r.get("lam"),
                        "qty": r.get("qty"), "risk": r.get("risk_per_contract"), "veto": r.get("veto")})
        return out[:8] or None
    except Exception:
        return None


def _emit(conn, tkr, dec, mech, book, now, chip_read=None, bookkey=None, role="primary"):
    """Resolve the ATM contract off the live chain, gate on spread, and emit a chip signal via ingest().
    Returns None on success, or a (state, reason) tuple when it declines — so the observer sees WHY."""
    server = _srv()
    cr = chip_read if (chip_read and chip_read.get("available")) else None
    if cr:                                              # use the FROZEN preview decision → preview == fill
        strike = cr["strike"]["strike"]
        exp, dte_lbl = cr["dte"]["expiry"], cr["dte"]["label"]
    else:                                               # safety fallback (should not happen from the scan loop)
        spot = MK.spot_quote(tkr, ib=conn)
        if not spot:
            return ("wait", "trigger hit but no underlying quote")
        inc = mech.get("strike_inc", 1.0)
        strike = round(spot / inc) * inc
        exp = now.strftime("%Y%m%d") if now.hour < 13 else (now + timedelta(days=1)).strftime("%Y%m%d")
        dte_lbl = "0DTE" if now.hour < 13 else "1DTE"
    strike_s = str(int(strike)) if float(strike).is_integer() else str(strike)
    q = MK.option_quote(tkr, strike_s, dec["cp"], expiry=exp, ib=conn)
    if not q or q.get("error") or not q.get("mid"):
        return ("wait", f"trigger hit but no quote on {strike_s}{dec['cp']}")
    entry_px = q.get("ask") or q.get("mid")             # a real fillable price -> verified, no price_mismatch
    if not entry_px:
        return ("wait", "trigger hit but no fillable price")
    if (q.get("spread_pct") or 99) > mech["spread_max"]:
        return ("wait", f"trigger hit but spread {q.get('spread_pct')}% > {mech['spread_max']}% — too wide")
    news = _news(tkr)
    # freeze the WHY at fill (strike/DTE verdict + tape decomposition + sweet-spot) so the live card can
    # tell the story's beginning; read back by server._chip_lifecycle via card["chip"].
    entry_read = None
    if chip_read and chip_read.get("available"):
        entry_read = {"strike": chip_read["strike"], "dte": chip_read["dte"],
                      "tape_at_entry": {"score": chip_read["tape"]["score"], "grade": chip_read["tape"]["grade"],
                                        "factors": chip_read["tape"]["factors"]},
                      "trigger": chip_read.get("trigger"),
                      "fill": {"spread_pct": chip_read["fill"]["spread_pct"],
                               "quality": chip_read["fill"]["quality"], "px": entry_px}}
    data = {
        "text": f"{tkr} {strike_s}{dec['cp']} @ {round(entry_px, 2)} {dte_lbl}",
        "provider": "chip",
        "msg_id": f"chip-{tkr}-{int(now.timestamp())}",
        "force": True,                                  # deliberate re-entry -> bypass the 4h/48h dedup sweep
        "chip_plan": {**{k: mech[k] for k in ("stop_pct", "t1_pct", "t2_pct", "t3_pct")},
                      "max_hold_min": MAX_HOLD_S // 60,    # real time stop — see MAX_HOLD_S
                      # EXIT MODEL, chip-scoped. db._exit_model('chip') is the authority that scores it;
                      # this is the same value carried on the card so the UI and the score cannot drift.
                      # The 3-tier ladder capped half the position at t1 and moved the stop to breakeven
                      # while the loss side still ran the full stop — negative carry on a near-driftless
                      # process. Measured on real quotes at the DTE chip trades (paired, day-block SE):
                      # SPY +0.0014R +/- 0.0011, QQQ +0.0022R +/- 0.0097 for the single exit. Direction
                      # agrees with both synthetic reviews; magnitude is ~10x smaller than the +0.0135R
                      # the SPY review reported, because THAT arm was coded as t1=stop with t2 unreachable
                      # (still half out at t1, then a breakeven stop) — it never removed the scale-out.
                      # Trailing was tested and is NOT shipped: negative on both tickers.
                      "exit_model": "single"},
        "chip_meta": {"regime": dec["regime"], "reason": dec["reason"], "score": dec.get("score"),
                      "tape_gate": "retired" if not TAPE_GATE_ENABLED else "enforced",
                      "live_grade": dec.get("live_grade"), "strict_take": dec.get("strict_take"),
                      "direction_role": role,          # 'primary' (stronger side) vs 'counter' (data only)
                      "geom": GEOM,                    # stop-geometry cohort — never pool g1 (0DTE) with g2
                      "news": news.get("state", "none"), "entry_read": entry_read,
                      # THE ROAD NOT TAKEN. The 4 unchosen rungs are ALREADY streaming from IBKR and were
                      # being discarded. Recording them turns every emit into a PAIRED within-setup
                      # comparison — same minute, same tape, same direction, only the strike differs —
                      # which cancels market-regime variance entirely. That is the only design that can
                      # resolve a small strike effect at ~5 signals/day; the unpaired version needs
                      # ~29,000 trades. Costs nothing: no extra quote, no extra line, ~200 bytes/emit.
                      "rungs_not_taken": _rungs_snapshot(cr)},
    }
    if os.environ.get("DESK_ENV") == "test":
        data["_allow_test_ingest"] = True               # sandbox only; prod bypasses the guard
    try:
        entry = server.ingest(data)
    except Exception as e:
        print(f"🤖 chip emit error {tkr}: {e}", flush=True)
        return ("wait", f"emit error: {e}")
    if entry and entry.get("id"):
        bk = bookkey or tkr
        book[bk] = {"sid": entry["id"], "opened": time.time(), "cp": dec["cp"]}
        _LAST_EMIT[bk] = now.timestamp()
        line = f"{tkr} {strike_s}{dec['cp']} @ {entry_px}  ({dec['reason']}, tape {dec.get('score')}, {role})"
        _activity("📈 SCALP " + line)
        print(f"🤖 chip {line} → {entry['id']}", flush=True)
        return None
    return ("wait", "trigger hit but ingest declined the card")


# ─────────────────────────── scan loop (daemon thread) ───────────────────────────

# ── THESIS EXPERIMENT (Phase 2/3) ────────────────────────────────────────────────────────────────────
# A fixed 10-minute clock grid, SPY and QQQ STAGGERED by 5 minutes. 74 proposals/day, ~$0.37/day.
#
# WHY 10 MINUTES AND NOT 30, which was my first instinct: day-blocking only discounts an observation when
# the HORIZON EXCEEDS THE SPACING. MEASURED lag-1 correlation of adjacent same-day 20-min forward returns
# at 30-min spacing is -0.036 — there is nothing to discount, and eff_n tracks the proposal count 1:1 up
# to ~25/day. My "days, not theses/day" reasoning was a correct lesson from the 33-second path data
# (~36x overlap) applied outside its domain. Cost of that error: 340 sessions to breakeven resolution
# instead of ~135. Nine months, for $0.24/day.
#
# STAGGERED because corr(SPY,QQQ) at the same timestamp is +0.919 — a simultaneous second ticker is worth
# ~0.4 of an independent observation, a second time slot is worth ~1.0.
#
# A FIXED grid also balances time-of-day BY CONSTRUCTION, so no blackout is needed and none is applied:
# time-of-day is undecidable on this desk's data, and excluding hours we cannot justify excluding would
# bias the sample before it starts.
THESIS_GRID = {"SPY": (9 * 60 + 35, 15 * 60 + 35), "QQQ": (9 * 60 + 40, 15 * 60 + 40)}
THESIS_STEP_MIN = 10
RESOLVE_EVERY_S = 900                       # settle pending theses every 15 min of session time
_THESIS_FIRED = {}                          # {"YYYYMMDD": {(ticker, slot_min), ...}}
_LAST_RESOLVE = [0.0]


def _thesis_slot(ticker, now):
    """The grid slot this ticker is currently in, or None. Fires on the first 5-second lap at/after the
    slot rather than demanding an exact minute match, so a slow lap can never skip a slot."""
    span = THESIS_GRID.get(ticker)
    if not span:
        return None
    lo, hi = span
    m = now.hour * 60 + now.minute
    if m < lo or m > hi:
        return None
    return lo + THESIS_STEP_MIN * ((m - lo) // THESIS_STEP_MIN)


def _maybe_thesis(tkr, ind, spot, now, a, mech):
    """Propose once per grid slot, ASYNCHRONOUSLY. Unconditional by design — NOT gated on the tape score,
    which measured silent-to-anti-predictive; conditioning a clean experiment on a known-bad filter would
    make the sample unrepresentative. The covariates are snapshotted instead, so the conditioning can be
    done post-hoc against the record."""
    try:
        slot = _thesis_slot(tkr, now)
        if slot is None or not spot:
            return
        day = now.strftime("%Y%m%d")
        fired = _THESIS_FIRED.setdefault(day, set())
        if len(_THESIS_FIRED) > 3:                       # bounded: keep only the last few days
            for k in sorted(_THESIS_FIRED)[:-3]:
                _THESIS_FIRED.pop(k, None)
        key = (tkr, slot)
        if key in fired:
            return
        fired.add(key)
        import chip_thesis as CT
        ctx = {"spot": spot, "time_et": now.strftime("%H:%M"), "slot_id": f"{day}-{tkr}-{slot}",
               "session_min": _session_min(now), "vwap": ind.get("vwap"), "rsi": ind.get("rsi"),
               "ema9": ind.get("ema9"), "ema21": ind.get("ema21"),
               "ema135": ind.get("ema135"), "ema315": ind.get("ema315"),
               "recent_closes": [round(c, 2) for c in (ind.get("closes") or [])[-20:]],
               "tape_score": a.get("score"), "tape_grade": a.get("grade"),
               "chip_verdict": a.get("verdict"), "regime_live": a.get("regime"),
               "news": (_news(tkr) or {}).get("state")}
        CT.propose_async(tkr, ctx, signal_id=ctx["slot_id"])
    except Exception:
        pass


def scan_loop():
    conn = None
    book = {}
    print("🤖 chip desk: scan loop started (shadow-only provider 'chip').", flush=True)
    _rebuild_book(book)          # adopt scalps that were still open across a restart
    while True:
        try:
            if conn is None:
                conn = MK.connect(client_id=82)
            now = _now_et()
            smin = _session_min(now)
            SCAN["ts"] = now.timestamp()
            if smin is None:                            # market closed -> reset intraday state, idle
                _PREV_SPOT.clear()
                _LAST_CROSS.clear()
                book.clear()
                STATUS.clear()
                SCAN["session"] = "closed"
                time.sleep(30)
                continue
            SCAN["session"] = "open"
            mechs = _load_mechanics()
            server = _srv()
            for tkr in TICKERS:
                try:
                    ind = _tape(conn, tkr)                           # chip's own 1-min tape (session VWAP)
                    spot = MK.spot_quote(tkr, ib=conn)               # FRESH per lap — cross detection + observer
                    mech = mechs.get(tkr, DEFAULT_MECHANICS.get(tkr, {}))
                    ps, vwap = _PREV_SPOT.get(tkr), ind.get("vwap")
                    _fresh_up = bool(ps is not None and spot and vwap and ps <= vwap and spot > vwap)
                    _fresh_dn = bool(ps is not None and spot and vwap and ps >= vwap and spot < vwap)
                    if _fresh_up or _fresh_dn:                   # remember the cross; it stays valid a while
                        _LAST_CROSS[tkr] = (now.timestamp(), "up" if _fresh_up else "down")
                    _lc = _LAST_CROSS.get(tkr)
                    _recent = bool(_lc and (now.timestamp() - _lc[0]) <= CROSS_WINDOW_S)
                    crossed_up = bool(_recent and _lc[1] == "up")
                    crossed_down = bool(_recent and _lc[1] == "down")
                    sdir = ("up" if spot > ps else "down" if spot < ps else None) if (spot and ps) else None
                    if spot:
                        _PREV_SPOT[tkr] = spot
                    news = _news(tkr)
                    # SHADOW BOTH DIRECTIONS independently (anti-correlated, so usually only one qualifies).
                    aC = assess(tkr, ind, mech, smin, "C", crossed_up, crossed_down)
                    aP = assess(tkr, ind, mech, smin, "P", crossed_up, crossed_down)
                    strong_side = "C" if (aC.get("score") or 0) >= (aP.get("score") or 0) else "P"
                    # THESIS EXPERIMENT: once per ticker per grid slot, async, direction-agnostic (the
                    # model picks its own direction, so it is proposed from the ticker's state, not a side).
                    _maybe_thesis(tkr, ind, spot, now, aC if strong_side == "C" else aP, mech)
                    exps = None
                    for side, a in (("C", aC), ("P", aP)):
                        key = f"{tkr}|{side}"
                        _release_if_done(book, key)
                        held = book.get(key)
                        chip_read = live_read = None
                        if held and spot:                            # IN a scalp -> live tape for the thesis check
                            live_read = build_live_read(ind, spot, side, held["sid"], now.timestamp())
                        if not held and spot:                        # build this side's reasoning + prospective option
                            inc = mech.get("strike_inc", 1.0)
                            if exps is None:
                                try: exps = MK.list_expiries(conn, tkr)
                                except Exception: exps = []
                            dt = dte_choice(tkr, mech, now, exps)
                            # ladder pick when ARMED (veto-only); plain ATM otherwise
                            pq, _strike, _lad = pick_strike(conn, tkr, side, spot, mech, dt, now,
                                                            a.get("score"), crossed_up or crossed_down)
                            # _lad is None  -> side not armed: plain ATM quote, unchanged behaviour.
                            # _lad is not None with no quote -> the ladder REFUSED (risk cap / all vetoed).
                            # Those are different, and collapsing them is what made the cap fail open.
                            if pq is None and _lad is None:
                                _strike = round(spot / inc) * inc
                                _ss = str(int(_strike)) if float(_strike).is_integer() else str(_strike)
                                try:
                                    pq = MK.option_quote(tkr, _ss, side, expiry=dt[1], ib=conn)
                                except Exception:
                                    pq = None
                            a = {**a, "ladder": _lad}
                            if pq is None and _lad is not None:
                                a = {**a, "verdict": "WAIT",
                                     "reason": _lad.get("note") or "no strike cleared the risk/veto checks"}
                            a = apply_extension(a, ind, mech, spot, pq, side)   # runway gate, needs the quote
                            chip_read = build_chip_read(ind, mech, spot, pq, a, now, smin, _strike, dt)
                        if held:
                            sig = next((s for s in server.SIGNALS if s.get("id") == held["sid"]), None)
                            pnl = ((sig or {}).get("shadow") or {}).get("now")
                            state = "holding"
                            reason = f"holding {side} scalp" + (f" · {pnl:+.0f}%" if pnl is not None else "")
                        elif _cooling(key, now):
                            left = int((COOLDOWN_S - (now.timestamp() - _LAST_EMIT.get(key, 0))) / 60) + 1
                            state, reason = "cooldown", f"cooldown ~{left}m after last {side} scalp"
                        elif news.get("veto"):
                            state, reason = "veto", f"news veto — {news.get('note') or 'event risk'}"
                        else:
                            state = "scan" if a["verdict"] == "TAKE" else "wait"
                            reason = a["reason"]
                        STATUS[key] = {"ticker": tkr, "side": side, "spot": spot, "spot_dir": sdir,
                                       "score": a.get("score"), "grade": a.get("grade"),
                                       "regime": a.get("regime"), "cp": side, "primary": side == strong_side,
                                       "state": state, "reason": reason, "news": news.get("state", "none"),
                                       "chip_read": chip_read, "live_read": live_read}
                        if state == "scan":
                            role = "primary" if side == strong_side else "counter"
                            r = _emit(conn, tkr, a, mech, book, now, chip_read, key, role)
                            if r:
                                STATUS[key]["state"], STATUS[key]["reason"] = r
                except Exception:
                    continue
            # THESIS EXPERIMENT: settle whatever is now coverable, reusing THIS connection (never open a
            # second IBKR handle on a 5-second loop). limit=None is mandatory — see horizon.resolve.
            try:
                if time.time() - _LAST_RESOLVE[0] > RESOLVE_EVERY_S:
                    _LAST_RESOLVE[0] = time.time()
                    import horizon as HZ
                    HZ.resolve(ib=conn, limit=None, write=True)
            except Exception:
                pass
            try:
                MK.reap_subs(conn, cap=80)
            except Exception:
                pass
            time.sleep(LOOP_SLEEP)
        except Exception as e:
            print(f"🤖 chip desk loop error: {e} — reconnecting", flush=True)
            conn = None
            time.sleep(10)
