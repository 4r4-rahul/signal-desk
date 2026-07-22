#!/usr/bin/env python3
"""
eval_harness.py — the MEASUREMENT MACHINE every desk review must use.

WHY THIS EXISTS
---------------
Three independent reviews of "chip" produced three incompatible answers to the same four questions.
The disagreements were not about the data; they were about the MEASUREMENT. Each review re-invented its
own replay, and each re-invention was optimistic in a way nobody caught until the next review disagreed:

  1. COST was taken as a single population median (4.545% of premium) over every provider, ticker and
     DTE in `path`. That median is set by names chip does not trade (PENG 50.0%, MU 46.2%, CELH 107.7%);
     9 tickers with median spread >10% supply 9.2% of the tick pool and drag the median ~5x above the
     SPY/QQQ ATM truth. A global constant cannot price a per-instrument frictional floor.

  2. ENTRY PRICE differed between the strategy arm and the null arm. chip's arm was marked ASK-in /
     BID-out (db._shadow_entry_px -> card_ask); the null arm was marked from bar/mid prices. The full
     spread was therefore charged to one arm and not the other, and the difference was then reported as
     "chip is worse than random". A strategy-vs-null comparison is only a statement about SELECTION if
     both arms are priced identically.

  3. STANDARD ERRORS were computed as if minutes were independent draws. They are not. The SPY/QQQ tick
     pool is 12 session days and 25 contract-paths; a 20-minute forward return sampled every ~33s
     overlaps its neighbours ~36-fold. iid-over-ticks SEs are optimistic by roughly sqrt(ticks/days),
     which is where z = -3.58 came from.

  4. THE NULL was built by a different code path from the strategy. Any difference in the two code paths
     lands in the comparison and is indistinguishable from edge.

The contract this module enforces, structurally rather than by convention:

  * `cost_model(ticker, premium)`   — per-instrument, measured, with provenance. No global constant.
  * `compare_arms(...)`             — takes ONE scorer and applies it to BOTH arms. There is no way to
                                      pass a different entry convention to the strategy and the null,
                                      because there is only one convention argument.
  * `block_bootstrap(...)`          — resamples DAYS (or contracts), never ticks. Every SE in every
                                      report must come from here.
  * `Arm.provenance`                — every result carries the exact filter, convention, horizon and
                                      block key that produced it, so two reviews that disagree can be
                                      diffed instead of argued about.

Nothing here touches the live desk: it is read-only against the DB and imports `market` only for
`tape_strength`. It never writes signals, never calls the broker, and never mutates `outcomes`.

USAGE
    import eval_harness as H
    pool = H.load_pool("SPY")                       # contract-paths with quotes + indicators
    res  = H.compare_arms(pool, H.gate_tape(62), horizon_min=20, convention="ask_bid")
    print(res)                                       # strategy, null, delta, all with block SEs
"""
from __future__ import annotations

import math
import random
import sqlite3
import statistics as _st
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

DB_PATH = "signal_desk.db"

# ─────────────────────────── 1. COST: per-instrument, measured ───────────────────────────
# MEASURED 2026-07-22 off `path` (this DB), two-sided quotes only, ATM defined as |delta| in [0.40,0.60]
# — the only options chip ever emits. `spread_pct` is the full bid/ask spread as a % of mid, which IS the
# ask-in/bid-out round-trip give-up. Commission is separate and is a $/contract charge, so it is a
# LARGER fraction of a cheap contract: it must be applied against the actual premium, never pre-baked
# into a percentage.
#
#   ticker   n_ticks   median spread%   mean%    penny-wide($0.01)
#   SPY        971        0.935          0.982      65.8%
#   QQQ        871        0.751          0.789      28.7%
#
# For contrast, the population median across ALL providers/tickers/DTEs is 4.545% (n=41,542) — the
# number the desk had been using. It is 4.9x the SPY truth and 6.1x the QQQ truth, and it is a
# statement about PENG and MU, not about SPY and QQQ.
SPREAD_PCT_ATM = {
    "SPY": 0.935,
    "QQQ": 0.751,
}
SPREAD_PCT_FALLBACK = 4.545      # the old population median — kept ONLY as the loud, wrong default for
                                 # an instrument we have not measured. If you see this in a result,
                                 # the instrument needs measuring, not this constant.

# tastytrade, per contract. Unchanged from db.py — restated here so the harness is self-contained and
# so a change in one place shows up as a diff against the other.
OPEN_COMM_PER_CT, OPEN_FEES_PER_CT = 1.00, 0.15
CLOSE_COMM_PER_CT, CLOSE_FEES_PER_CT = 0.00, 0.15
RT_COST_PER_CT = OPEN_COMM_PER_CT + OPEN_FEES_PER_CT + CLOSE_COMM_PER_CT + CLOSE_FEES_PER_CT   # $1.30


def cost_model(ticker, premium, observed_spread_pct=None):
    """Total round-trip friction as a % of premium paid, split into its two components.

    spread  — ask-in/bid-out give-up. Uses the OBSERVED spread when the tick carries a two-sided quote
              (always prefer the real quote over a table lookup); falls back to the per-instrument
              measured median; falls back loudly to the population median for unmeasured instruments.
    comm    — $1.30/contract expressed against THIS premium. A $1.30 charge is 0.62% of a $2.10
              contract and 0.26% of a $5.00 one; a percentage constant cannot represent it.

    Returns {'spread_pct','comm_pct','total_pct','source'}."""
    prem = float(premium or 0)
    if observed_spread_pct is not None and observed_spread_pct > 0:
        sp, src = float(observed_spread_pct), "observed_quote"
    elif ticker in SPREAD_PCT_ATM:
        sp, src = SPREAD_PCT_ATM[ticker], "measured_atm_median"
    else:
        sp, src = SPREAD_PCT_FALLBACK, "UNMEASURED_population_median"
    comm = (RT_COST_PER_CT / (prem * 100.0) * 100.0) if prem > 0 else 0.0
    return {"spread_pct": round(sp, 3), "comm_pct": round(comm, 3),
            "total_pct": round(sp + comm, 3), "source": src}


def friction_floor_r(ticker, premium, stop_pct, observed_spread_pct=None):
    """The frictional floor in R — what a trade with ZERO directional edge and zero holding time loses.
    This is the number a strategy must beat before it is worth anything, and it is per-instrument."""
    c = cost_model(ticker, premium, observed_spread_pct)
    return -c["total_pct"] / abs(float(stop_pct)), c


# ─────────────────────────── 2. THE TICK POOL ───────────────────────────
@dataclass
class Tick:
    signal_id: str
    ticker: str
    opt_type: str
    dte: int
    ts: datetime
    day: str                    # session date — the bootstrap block key
    bid: float
    ask: float
    mid: float
    delta: float
    spot: float
    rsi: float
    vwap: float
    ema9: float
    ema21: float
    bb_up: float
    bb_low: float
    tod_min: int

    @property
    def spread_pct(self):
        return (self.ask - self.bid) / self.mid * 100.0 if self.mid else None

    def ind(self):
        """Exactly the dict market.tape_strength consumes. server._path_tick writes these columns
        straight out of market.indicators(), so this reconstruction is the same object, not a proxy."""
        return {"price": self.spot, "rsi": self.rsi, "vwap": self.vwap, "ema9": self.ema9,
                "ema21": self.ema21, "bb_up": self.bb_up, "bb_low": self.bb_low}


def load_pool(ticker=None, atm_only=True, db_path=DB_PATH, require_indicators=True):
    """Contract-paths from `path`, grouped by signal_id and ordered in time.

    A "pool" is {signal_id: [Tick, ...]}. Forward returns are ONLY ever computed within one signal_id,
    because that is the only way two prices are the same contract. Cross-contract forward returns were
    never valid and are not expressible here.

    atm_only keeps |delta| in [0.40,0.60] — chip only ever buys ATM, so anything else is a different
    instrument with a different spread and a different theta."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = ("SELECT s.signal_id, s.ticker, s.type, s.dte, p.ts_utc, p.bid, p.ask, p.opt_mid, p.delta, p.spot, "
         "       p.rsi, p.vwap, p.ema9, p.ema21, p.bb_up, p.bb_low, p.tod_min "
         "FROM path p JOIN signals s ON s.signal_id = p.signal_id "
         "WHERE p.bid > 0 AND p.ask > 0 AND p.ask >= p.bid AND p.opt_mid > 0")
    args = []
    if ticker:
        q += " AND s.ticker = ?"; args.append(ticker)
    q += " ORDER BY s.signal_id, p.ts_utc"
    pool = defaultdict(list)
    for r in con.execute(q, args):
        (sid, tkr, typ, dte, ts, bid, ask, mid, delta, spot, rsi, vwap, e9, e21, bbu, bbl, tod) = r
        if atm_only and (delta is None or not (0.40 <= abs(delta) <= 0.60)):
            continue
        if require_indicators and (rsi is None or not vwap or not spot):
            continue
        t = datetime.fromisoformat(ts)
        pool[sid].append(Tick(sid, tkr, typ or "C", dte, t, ts[:10], bid, ask, mid, delta, spot,
                              rsi, vwap, e9, e21, bbu, bbl, tod))
    con.close()
    return {k: v for k, v in pool.items() if len(v) >= 2}


# ─────────────────────────── 3. GATES (arm definitions) ───────────────────────────
def tape_score(tick):
    """chip's entry score, reconstructed at an arbitrary stored tick."""
    import market as MK
    return (MK.tape_strength(tick.opt_type, tick.ind()) or {}).get("score")


def gate_tape(threshold=62):
    """chip's shipped entry gate. Returns a predicate over Tick."""
    def g(t):
        s = tape_score(t)
        return s is not None and s >= threshold
    g.name = f"tape>={threshold}"
    return g


def gate_all():
    def g(t):
        return True
    g.name = "null_all_minutes"
    return g


def gate_tod(lo_min, hi_min, inner=True):
    def g(t):
        if t.tod_min is None:
            return False
        within = lo_min <= t.tod_min < hi_min
        return within if inner else not within
    g.name = f"tod[{lo_min},{hi_min})" + ("" if inner else "_excluded")
    return g


# ─────────────────────────── 4. SCORING: ONE convention, both arms ───────────────────────────
CONVENTIONS = ("ask_bid", "mid_mid", "mid_bid")


def _entry_exit(tick_in, tick_out, convention):
    """(entry_price, exit_price) under a named convention.

    ask_bid  — tradeable: lift the offer, hit the bid. The honest cost of transacting.
    mid_mid  — frictionless mark. Charges NO spread to either side.
    mid_bid  — pay mid, exit bid. Half the spread.

    The whole point of this function is that `convention` is a single argument threaded to BOTH arms by
    compare_arms(). There is no per-arm override, so the fill-marking asymmetry that produced the
    "worse than random" headline cannot be reintroduced without editing this file."""
    if convention == "ask_bid":
        return tick_in.ask, tick_out.bid
    if convention == "mid_mid":
        return tick_in.mid, tick_out.mid
    if convention == "mid_bid":
        return tick_in.mid, tick_out.bid
    raise ValueError(f"unknown convention {convention!r}; use one of {CONVENTIONS}")


def score_entry(series, i, horizon_min, mech, convention="ask_bid", charge_commission=True,
                scaleout="ladder"):
    """R of entering at series[i] and running the exit model for horizon_min, on ONE convention.

    Uses db._shadow_scaleout so the harness scores the same discipline the live desk scores — if that
    changes, this changes with it and cannot silently diverge.

    scaleout: 'ladder'  = the shipped 1/2@t1 -> 1/4@t2 -> runner@t3
              'single'  = all out at t1, else stop, else last tick
              'trail'   = all out at t1; before t1, a stop that ratchets to breakeven once +t1/2 is seen
    Returns R, or None if the window has no data."""
    import db as DB
    t0 = series[i]
    entry, _ = _entry_exit(t0, t0, convention)
    if not entry or entry <= 0:
        return None
    end = t0.ts.timestamp() + horizon_min * 60
    fwd = [t for t in series[i + 1:] if t.ts.timestamp() <= end]
    if not fwd:
        return None
    ticks = []
    for t in fwd:
        _, ex = _entry_exit(t0, t, convention)
        if ex and ex > 0:
            ticks.append(round((ex / entry - 1) * 100, 2))
    if not ticks:
        return None
    stop, t1, t2, t3 = mech["stop_pct"], mech["t1_pct"], mech.get("t2_pct"), mech.get("t3_pct")
    if scaleout == "ladder":
        ret = DB._shadow_scaleout(ticks, t1, t2, stop, t3)
    elif scaleout == "single":
        ret = _single_exit(ticks, t1, stop)
    elif scaleout == "trail":
        ret = _trail_exit(ticks, t1, stop)
    else:
        raise ValueError(f"unknown scaleout {scaleout!r}")
    if ret is None:
        return None
    if charge_commission:
        ret -= RT_COST_PER_CT / (entry * 100.0) * 100.0
    return ret / abs(stop)


def _single_exit(ticks, t1, stop):
    """All out at +t1, full -1R at -stop, else mark at the last tick. No ladder, no re-entry."""
    slvl = -abs(stop)
    for pct in ticks:
        if pct >= t1:
            return float(t1)
        if pct <= slvl:
            return float(min(pct, slvl))
    return float(ticks[-1])


def _trail_exit(ticks, t1, stop):
    """All out at +t1; the stop ratchets to breakeven once +t1/2 has been seen. One decision, one
    ratchet — the cheapest thing that still protects a winner that rolls over."""
    slvl, armed = -abs(stop), False
    for pct in ticks:
        if pct >= t1:
            return float(t1)
        if not armed and pct >= t1 / 2.0:
            armed, slvl = True, 0.0
        if pct <= slvl:
            return float(min(pct, slvl))
    return float(ticks[-1])


# ─────────────────────────── 5. BLOCK BOOTSTRAP ───────────────────────────
def block_bootstrap(values, blocks, n_boot=4000, seed=7, stat=None):
    """SE and CI for a statistic, resampling BLOCKS with replacement — never individual observations.

    `blocks` is a parallel list of block keys (session day, or contract id). Observations inside a block
    are kept together, so the estimate inherits the within-day/within-contract correlation instead of
    pretending it away. This is the single correction that turns z = -3.58 into z ~ -0.6: forward returns
    sampled every 33s over a 20m window overlap ~36-fold, and there are only 12 independent days.

    Returns {'mean','se','lo','hi','n','n_blocks','eff_n'} where eff_n is the iid-equivalent sample size
    implied by the block SE — report it, because it is the honest N."""
    stat = stat or (lambda v: sum(v) / len(v))
    by = defaultdict(list)
    for v, b in zip(values, blocks):
        by[b].append(v)
    keys = list(by)
    if len(keys) < 2 or not values:
        return {"mean": (stat(values) if values else None), "se": None, "lo": None, "hi": None,
                "n": len(values), "n_blocks": len(keys), "eff_n": None}
    rng = random.Random(seed)
    point = stat(values)
    draws = []
    for _ in range(n_boot):
        samp = []
        for _ in range(len(keys)):
            samp.extend(by[rng.choice(keys)])
        if samp:
            draws.append(stat(samp))
    draws.sort()
    se = _st.pstdev(draws)
    sd = _st.pstdev(values) if len(values) > 1 else 0.0
    return {"mean": point, "se": se,
            "lo": draws[int(0.025 * len(draws))], "hi": draws[int(0.975 * len(draws))],
            "n": len(values), "n_blocks": len(keys),
            "eff_n": (round((sd / se) ** 2, 1) if se and sd else None)}


def naive_se(values):
    """The WRONG SE (iid over observations). Provided only so a report can show both and make the
    inflation factor explicit. Never quote this one on its own."""
    if len(values) < 2:
        return None
    return _st.pstdev(values) / math.sqrt(len(values))


# ─────────────────────────── 6. ARM COMPARISON ───────────────────────────
@dataclass
class Arm:
    name: str
    r: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    entry_spread: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def summary(self, n_boot=4000):
        bs = block_bootstrap(self.r, self.blocks, n_boot=n_boot)
        return {**bs, "name": self.name,
                "naive_se": naive_se(self.r),
                "se_inflation": (round(bs["se"] / naive_se(self.r), 2)
                                 if bs["se"] and naive_se(self.r) else None),
                "mean_entry_spread_pct": (round(_st.mean(self.entry_spread), 3)
                                          if self.entry_spread else None),
                "provenance": self.provenance}


def build_arm(pool, gate, horizon_min, mech, convention="ask_bid", scaleout="ladder",
              charge_commission=True, block_by="day", name=None):
    """Score every tick that passes `gate` — one arm, one convention, one exit model."""
    arm = Arm(name=name or getattr(gate, "name", "arm"))
    for sid, series in pool.items():
        for i, t in enumerate(series):
            if not gate(t):
                continue
            r = score_entry(series, i, horizon_min, mech, convention, charge_commission, scaleout)
            if r is None:
                continue
            arm.r.append(r)
            arm.blocks.append(t.day if block_by == "day" else sid)
            sp = t.spread_pct
            if sp is not None:
                arm.entry_spread.append(sp)
    arm.provenance = {"gate": getattr(gate, "name", "?"), "horizon_min": horizon_min,
                      "convention": convention, "scaleout": scaleout,
                      "commission": charge_commission, "block_by": block_by,
                      "mech": dict(mech), "n_contracts": len(pool)}
    return arm


def compare_arms(pool, strategy_gate, horizon_min, mech, null_gate=None, convention="ask_bid",
                 scaleout="ladder", charge_commission=True, block_by="day", n_boot=4000, seed=7):
    """Strategy vs null, END TO END IDENTICAL except for the gate.

    `convention`, `scaleout`, `charge_commission`, `horizon_min` and `mech` are single arguments used to
    build BOTH arms. That is the structural fix for contradiction (1): there is no signature by which a
    caller can price the strategy at the ask and the null at the mid.

    The delta is bootstrapped as a PAIRED statistic over shared blocks, so day-level market conditions
    cancel instead of adding noise to the difference."""
    null_gate = null_gate or gate_all()
    s = build_arm(pool, strategy_gate, horizon_min, mech, convention, scaleout,
                  charge_commission, block_by, name=f"STRATEGY {getattr(strategy_gate,'name','?')}")
    n = build_arm(pool, null_gate, horizon_min, mech, convention, scaleout,
                  charge_commission, block_by, name=f"NULL {getattr(null_gate,'name','?')}")

    # paired block bootstrap on the difference of means
    sb, nb = defaultdict(list), defaultdict(list)
    for v, b in zip(s.r, s.blocks): sb[b].append(v)
    for v, b in zip(n.r, n.blocks): nb[b].append(v)
    shared = sorted(set(sb) & set(nb))
    delta = None
    if len(shared) >= 2:
        rng = random.Random(seed)
        point = (_st.mean(s.r) - _st.mean(n.r))
        draws = []
        for _ in range(n_boot):
            ks = [rng.choice(shared) for _ in shared]
            a = [v for k in ks for v in sb[k]]
            b = [v for k in ks for v in nb[k]]
            if a and b:
                draws.append(_st.mean(a) - _st.mean(b))
        draws.sort()
        se = _st.pstdev(draws)
        delta = {"delta_r": round(point, 4), "se": round(se, 4) if se else None,
                 "lo": round(draws[int(0.025 * len(draws))], 4),
                 "hi": round(draws[int(0.975 * len(draws))], 4),
                 "z": (round(point / se, 2) if se else None),
                 "n_blocks": len(shared)}
    return {"strategy": s.summary(n_boot), "null": n.summary(n_boot), "delta": delta,
            "selectivity": (round(len(s.r) / len(n.r), 3) if n.r else None)}


def compare_configs(pool, gate, horizon_a, mech_a, horizon_b, mech_b, scaleout_a="ladder",
                    scaleout_b="ladder", convention="ask_bid", n_boot=4000, seed=13, block_by="day"):
    """PAIRED comparison of two exit/geometry configurations over the IDENTICAL entry set.

    Config changes must be measured paired, because the entry set is the dominant source of variance and
    it is common to both arms. An unpaired comparison of two configs on the same data throws that away
    and typically inflates the SE 5-20x — which is how a real +0.01R mechanics effect gets buried, and
    equally how a spurious one survives. Both prior reviews got this right; it is restated here so the
    next one cannot get it wrong.

    Returns the mean paired difference (b - a) with a day-block bootstrap SE on the DIFFERENCE."""
    diffs, blocks = [], []
    for sid, series in pool.items():
        for i, t in enumerate(series):
            if not gate(t):
                continue
            ra = score_entry(series, i, horizon_a, mech_a, convention, True, scaleout_a)
            rb = score_entry(series, i, horizon_b, mech_b, convention, True, scaleout_b)
            if ra is None or rb is None:
                continue
            diffs.append(rb - ra)
            blocks.append(t.day if block_by == "day" else sid)
    if not diffs:
        return {"delta_r": None, "n": 0}
    bs = block_bootstrap(diffs, blocks, n_boot=n_boot, seed=seed)
    return {"delta_r": round(bs["mean"], 4), "se": round(bs["se"], 4) if bs["se"] else None,
            "lo": round(bs["lo"], 4) if bs["lo"] is not None else None,
            "hi": round(bs["hi"], 4) if bs["hi"] is not None else None,
            "z": (round(bs["mean"] / bs["se"], 2) if bs["se"] else None),
            "n": bs["n"], "n_blocks": bs["n_blocks"],
            "naive_se": round(naive_se(diffs), 4) if len(diffs) > 1 else None}


# ─────────────────────────── 7. POWER ───────────────────────────
def trades_needed(effect_r, dispersion_r=0.55, alpha=0.05, power=0.80):
    """N trades to detect `effect_r` at the given dispersion. Two-sided.

    This is the number that decides which questions are answerable at ~5 signals/day and which are not.
    It is deliberately the FIRST thing any review should compute, not the last."""
    z_a, z_b = 1.959963985, 0.8416212336
    n = ((z_a + z_b) * dispersion_r / effect_r) ** 2
    return {"effect_r": effect_r, "dispersion_r": dispersion_r, "n_trades": math.ceil(n),
            "days_at_5_per_day": math.ceil(n / 5), "years_at_5_per_day": round(n / 5 / 252, 2)}


def detectable_effect(n_trades, dispersion_r=0.55, alpha=0.05, power=0.80):
    """Inverse: the SMALLEST effect a given sample can see. Use it to say what a result rules out."""
    z_a, z_b = 1.959963985, 0.8416212336
    return round((z_a + z_b) * dispersion_r / math.sqrt(n_trades), 4)


# ─────────────────────────── 8. SELF-CHECK ───────────────────────────
def audit_pool(pool):
    """The honest N. Print this at the top of every review."""
    days = sorted({t.day for s in pool.values() for t in s})
    ticks = sum(len(s) for s in pool.values())
    return {"n_contracts": len(pool), "n_ticks": ticks, "n_days": len(days),
            "days": days,
            "ticks_per_contract_median": (_st.median([len(s) for s in pool.values()]) if pool else 0)}


if __name__ == "__main__":
    for tk in ("SPY", "QQQ"):
        p = load_pool(tk)
        print(f"{tk}: {audit_pool(p)}")
        print(f"   friction floor: {friction_floor_r(tk, 2.10, 26 if tk=='SPY' else 28)}")
