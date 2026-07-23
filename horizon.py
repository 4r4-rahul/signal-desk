#!/usr/bin/env python3
"""
horizon.py — Phase 3: resolve every recorded thesis at MULTIPLE horizons and let the data say which one pays.

This closes the loop the owner asked for: "capture everything, and the thesis will say whether it is
workable for minutes or days." Phase 2 records a falsifiable claim, outcome-blind. This module settles it
against real bars at 20m / 1h / 4h / ~5 sessions, writes an OUTCOME event per horizon into the immutable
ledger, and reports the hit rate and forward drift per horizon with DAY-BLOCK standard errors.

WHY DAY-BLOCK, restated because it has already burned this desk once: theses cluster within sessions, so
treating them as independent draws inflates z by ~3x. That error produced a "z = -3.58" headline that had
to be publicly retracted. eval_harness.block_bootstrap is reused here rather than reimplemented, so the
same discipline applies by construction.

WHAT THIS CANNOT DO: it cannot make a small edge visible. eval_harness.trades_needed() is reported
alongside every result precisely so a tempting number is always shown next to the sample it would take to
believe it. At ~5 theses/day a +0.03R effect needs ~2 years; that fact is printed, not buried.

RESOLUTION IS DETERMINISTIC. No model output is consulted here. The thesis made a claim; bars settle it.
"""
import json, math
from datetime import datetime, timezone, timedelta

HORIZONS_MIN = (20, 60, 240, 1950)
_BARCACHE = {}


_BAR_TTL_S = 300


def _bars(ib, ticker, duration="10 D", size="1 min"):
    """Cached bars with a TTL, and NEVER caching a failure. The first version stored [] on a transient
    IBKR error and pinned it for the process lifetime, so every later thesis silently failed to resolve;
    it also never refreshed, so newly-proposed theses could not resolve in a long-lived process."""
    import time as _t
    key = (ticker, duration, size)
    hit = _BARCACHE.get(key)
    if hit and (_t.time() - hit[0]) < _BAR_TTL_S and hit[1]:
        return hit[1]
    import market as MK
    try:
        got = MK.get_bars(ib, ticker, duration=duration, size=size) or []
    except Exception:
        got = []
    if got:
        _BARCACHE[key] = (_t.time(), got)
    return got or (hit[1] if hit else [])


def _utc(bt):
    if bt is None:
        return None
    if not hasattr(bt, "tzinfo"):                     # a date (daily bars), not a datetime -> unusable here
        return None
    return bt.replace(tzinfo=timezone.utc) if bt.tzinfo is None else bt.astimezone(timezone.utc)


def _fwd_return(bars, t0_utc, horizon_min):
    """Underlying % move over `horizon_min` TRADING minutes, counted by BAR INDEX in the RTH series.

    Wall-clock arithmetic was wrong here and dangerously so. get_bars uses useRTH=True, so a real 2-day
    SPY pull has a 1051-minute hole between 15:59 and the next 09:30. Adding timedelta(minutes=20) and
    taking "the first bar at/after" meant:
      · >50% of '20-minute' returns actually spanned the overnight gap;
      · a proposal after the close resolved px0 and px1 to the SAME bar and returned a FABRICATED exact
        0.0, which then scored as a real observation (hit=False) and dragged the mean toward zero;
      · a thesis older than the bar window resolved to bars[0] for both legs — also a fake 0.0;
      · the '1950m ~ 5 sessions' horizon was really ~2 sessions (32.5 wall-clock hours).
    Indexing forward N bars makes horizon_min mean exactly what it says, and makes 'not enough data' an
    honest None instead of a zero. An unresolved thesis MUST stay unresolved."""
    if not bars or not horizon_min:
        return None
    try:
        t0 = _utc(datetime.fromisoformat(t0_utc))
    except Exception:
        return None
    if t0 is None:
        return None
    first = _utc(getattr(bars[0], "date", None))
    if first is None or first > t0:
        return None                                    # thesis predates our bar window -> cannot resolve
    i0 = None
    for i, b in enumerate(bars):
        bt = _utc(getattr(b, "date", None))
        if bt is not None and bt >= t0:
            i0 = i
            break
    if i0 is None:
        return None                                    # no bar at/after the proposal
    i1 = i0 + int(horizon_min)                         # 1-min RTH bars: index delta == trading minutes
    if i1 >= len(bars) or i1 == i0:
        return None                                    # window not fully covered -> honest None
    try:
        px0, px1 = float(bars[i0].close), float(bars[i1].close)
    except (TypeError, ValueError):
        return None
    if not px0 or px0 <= 0:
        return None
    # N trading minutes forward is the right COUNT, but a window that crosses a session boundary carries
    # overnight gap risk and has a materially different distribution from an intraday one. Flag it rather
    # than drop it: dropping would bias the sample toward mid-session proposals.
    d0, d1 = _utc(bars[i0].date), _utc(bars[i1].date)
    crossed = bool(d0 and d1 and d0.date() != d1.date())
    return {"pct": (px1 / px0 - 1.0) * 100.0, "crossed_session": crossed}


def resolve(ib=None, limit=None, write=True):   # limit=None is MANDATORY, not stylistic
    """Settle every pending thesis at every horizon. One OUTCOME per (thesis, horizon); idempotent.

    limit MUST stay None. events_of_kind is ORDER BY seq ASC — OLDEST FIRST — so any LIMIT means that once
    the oldest N theses are resolved, pending() returns ZERO while unresolved theses exist beyond the
    window. Verified: with 520 theses and limit=500, pending() returned 0 with 20 outstanding. At 74/day
    that silently kills the experiment on session 7, with no error anywhere."""
    import chip_thesis as CT
    own = ib is None
    if own:
        try:
            import market as MK
            ib = MK.connect(client_id=79)
        except Exception:
            ib = None
    settled = []
    try:
        for ev, todo in CT.pending(limit):
            try:
                p = ev.get("payload") or {}
                tk, t0 = p.get("ticker"), p.get("proposed_utc")
                direction = p.get("direction")
                if not tk or not t0:
                    continue
                bars = _bars(ib, tk) if ib else []
                for h in todo:                       # ONE OUTCOME PER HORIZON -> long horizons can populate
                    res = _fwd_return(bars, t0, h)
                    if res is None:
                        continue                     # not yet coverable; stays pending, resolves later
                    fwd = res["pct"]
                    # signed in the THESIS's favour: a SHORT that falls is a win.
                    signed = fwd if direction == "LONG" else (-fwd if direction == "SHORT" else 0.0)
                    rec = {"ticker": tk, "direction": direction, "conviction": p.get("conviction"),
                           "regime": p.get("regime"), "horizon_claimed": p.get("horizon_min"),
                           "horizon_min": h,
                           # carried forward so report() day-blocks on the TRADING day, not the ledger
                           # write time — without it a multi-day sample collapses into ONE block and the
                           # clustering correction silently does nothing.
                           "proposed_utc": t0,
                           "fwd_pct": round(fwd, 4), "signed_pct": round(signed, 4),
                           "crossed_session": res["crossed_session"],
                           "hit": bool(signed > 0) if direction in ("LONG", "SHORT") else None,
                           "resolved_utc": datetime.now(timezone.utc).isoformat()}
                    if write:
                        try:
                            import playbook as PB
                            PB.append("OUTCOME", signal_id=ev.get("signal_id"), actor="system",
                                      horizon=str(h), **rec)
                        except Exception:
                            pass
                    settled.append(rec)
            except Exception:
                continue                             # one malformed thesis must not kill the whole run
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass
    return settled


FLAG_HORIZONS = (20, 60)     # h=240/1950 are RECORDED but never flagged: a 1950-min window spans five
                             # sessions, so day-blocking is formally invalid there (eff_n is ~26 over 29
                             # sessions REGARDLESS of cadence, true independent count ~6). Week-blocking is
                             # the right estimator and needs 15+ weekly blocks (~4 months) to mean anything.
                             # Flagging 2 instead of 4 also lifts the Holm threshold 0.05/4 -> 0.05/2.
# Per-ticker 20-min sd, MEASURED over 29 real sessions. Raw pooling lets QQQ's 1.75x dispersion dominate
# and silently inflates eff_n; normalising puts both tickers on one scale.
VOL_NORM_20M = {"SPY": 0.149, "QQQ": 0.261}
MIN_BLOCKS_FOR_SIG = 15      # below ~15 independent days the block bootstrap's CI is far too narrow:
MIN_EFF_N_FOR_SIG = 10       # measured false-positive rate on NULL data was 49.8% at 2 days, 27.2% at 3,
                             # 17.7% at 5, 9.9% at 10 — against a nominal 5%. The flag is what the module
                             # tells you to trust, so an over-firing flag is worse than no flag at all.


def _p_from(mean, se):
    """Two-sided normal p from the block-bootstrap SE. Approximate, and deliberately so — it exists to
    RANK horizons for a Holm correction, not to be quoted as an exact p."""
    if not se or se <= 0 or mean is None:
        return 1.0
    z = abs(mean) / se
    return math.erfc(z / math.sqrt(2.0))


def report(limit=2000):
    """Per-horizon hit rate and mean signed drift, with DAY-BLOCK bootstrap SEs, plus the sample each
    result would need to be believed. Reads only the ledger — no market access, no model."""
    try:
        import playbook as PB, eval_harness as EH
    except Exception:
        return None
    # _BARCACHE must not cache a FAILURE forever: one transient IBKR error otherwise pins [] for the
    # process lifetime and no thesis can ever resolve again.
    # indexed read + DE-DUP by (signal_id, horizon): a duplicate OUTCOME row must never be counted twice.
    seen, evs = set(), []
    for e in PB.events_of_kind("OUTCOME", limit=limit):
        p = e.get("payload") or {}
        if not isinstance(p, dict) or p.get("signed_pct") is None:
            continue                              # one malformed row must not kill the whole report
        key = (e.get("signal_id"), e.get("horizon"))
        if key in seen:
            continue
        seen.add(key)
        evs.append(e)
    if not evs:
        return {"n": 0, "horizons": {}, "note": "no resolved theses yet"}
    out = {}
    for h in HORIZONS_MIN:
        vals, blocks, hits, declines, drift_terms = [], [], [], 0, []
        for e in evs:
            p = e["payload"]
            if int(p.get("horizon_min") or -1) != h:
                continue
            if p.get("direction") == "NONE":
                declines += 1                     # recorded, and reported — but not a directional hit
                continue
            # DRIFT DE-MEANING, free and necessary. QQQ's unconditional 20-min drift is -1.50 bp, so an
            # always-short model books fake "edge" LARGER than the 8-week MDE. edge = signed - sign*mean(fwd)
            # removes the market's own move using the unsigned fwd_pct already in every payload, on the same
            # timestamps, so the regime cancels. This is the paired design — with no extra proposals.
            sgn = 1.0 if p.get("direction") == "LONG" else -1.0
            raw_signed = p["signed_pct"]
            sd = VOL_NORM_20M.get(p.get("ticker"), 1.0) or 1.0
            vals.append(raw_signed / sd)                 # vol-normalised so SPY and QQQ pool honestly
            drift_terms.append((sgn, (p.get("fwd_pct") or 0.0) / sd))
            blocks.append(((p.get("proposed_utc") or e.get("ts") or "")[:10]) or "?")
            if p.get("hit") is not None:
                hits.append(1 if p["hit"] else 0)
        if not vals:
            out[h] = {"n": 0, "declines": declines}
            continue
        mean_raw = sum(vals) / len(vals)
        # subtract mean(sign) * mean(fwd): the drift a zero-skill model of this long/short mix would book
        if drift_terms:
            msign = sum(x[0] for x in drift_terms) / len(drift_terms)
            mfwd = sum(x[1] for x in drift_terms) / len(drift_terms)
            vals = [v - msign * mfwd for v in vals]
        mean = sum(vals) / len(vals)
        try:
            bs = EH.block_bootstrap(vals, blocks) or {}
        except Exception:
            bs = {}
        lo, hi, se = bs.get("lo"), bs.get("hi"), bs.get("se")
        nb, eff = bs.get("n_blocks") or 0, bs.get("eff_n")
        if eff is not None:
            eff = min(eff, float(len(vals)))   # you cannot have more EFFECTIVE samples than observations;
                                               # a degenerate near-zero SE otherwise reports millions
        # UNDERPOWERED unless there are enough INDEPENDENT days. Measured FPR on null data: 49.8% @ 2
        # blocks, 9.9% @ 10, 6.2% @ 40. Gating here is the difference between a flag that means something
        # and one that fires on noise one time in five.
        powered = bool(h in FLAG_HORIZONS and nb >= MIN_BLOCKS_FOR_SIG
                       and (eff or 0) >= MIN_EFF_N_FOR_SIG and se and se > 0)
        out[h] = {"n": len(vals), "declines": declines, "mean_signed_pct": round(mean, 4),
                  "mean_before_demean": round(mean_raw, 4), "vol_normalised": True,
                  "se": round(se, 4) if se else None,
                  "ci": [round(lo, 4), round(hi, 4)] if lo is not None else None,
                  "eff_n": eff, "n_days": len(set(blocks)),
                  "hit_rate": round(100.0 * sum(hits) / len(hits), 1) if hits else None,
                  "powered": powered, "p_raw": round(_p_from(mean, se), 4) if powered else None,
                  "significant": False}
    # HOLM across the horizons actually tested — report() runs the flag on 4 correlated windows, so an
    # uncorrected per-cell test inflates the family-wise error to ~25% at small samples.
    cand = sorted([(v["p_raw"], k) for k, v in out.items() if v.get("powered") and v.get("p_raw") is not None])
    m = len(cand)
    for i, (pv, k) in enumerate(cand):
        if pv <= 0.05 / (m - i):
            out[k]["significant"] = True
        else:
            break                                  # Holm stops at the first failure
    best = None
    for h, r in out.items():
        if r.get("n") and r.get("mean_signed_pct") is not None:
            if best is None or r["mean_signed_pct"] > out[best]["mean_signed_pct"]:
                best = h
    return {"n": len(evs), "horizons": out, "best_horizon_min": best,
            "caveat": ("mean drift is not P&L — it must clear the ~1.7bp round trip before it pays, and "
                       "no horizon should be believed until 'significant' is true on a day-block CI")}


if __name__ == "__main__":
    print(json.dumps(report(), indent=2, default=str))
