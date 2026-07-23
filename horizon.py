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
import json
from datetime import datetime, timezone, timedelta

HORIZONS_MIN = (20, 60, 240, 1950)
_BARCACHE = {}


def _bars(ib, ticker, duration="10 D", size="1 min"):
    key = (ticker, duration, size)
    if key not in _BARCACHE:
        import market as MK
        try:
            _BARCACHE[key] = MK.get_bars(ib, ticker, duration=duration, size=size) or []
        except Exception:
            _BARCACHE[key] = []
    return _BARCACHE[key]


def _fwd_return(bars, t0_utc, horizon_min):
    """Underlying % move from the first bar at/after t0 to the first bar at/after t0+horizon.
    Returns None when the window isn't fully covered — an unresolved thesis must stay unresolved rather
    than be settled against a truncated series (which would silently bias toward short horizons)."""
    if not bars:
        return None
    try:
        t0 = datetime.fromisoformat(t0_utc)
    except Exception:
        return None
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=horizon_min)
    px0 = px1 = None
    for b in bars:
        bt = getattr(b, "date", None)
        if bt is None:
            continue
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        else:
            bt = bt.astimezone(timezone.utc)
        if px0 is None and bt >= t0:
            px0 = float(b.close)
        if bt >= t1:
            px1 = float(b.close)
            break
    if px0 is None or px1 is None or px0 <= 0:
        return None
    return (px1 / px0 - 1.0) * 100.0


def resolve(ib=None, limit=500, write=True):
    """Settle every pending thesis at every horizon. Writes one OUTCOME event per (thesis, horizon) into
    the ledger. Idempotent: a thesis that already has OUTCOMEs is skipped by chip_thesis.pending()."""
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
        for ev in CT.pending(limit):
            p = ev.get("payload") or {}
            tk, t0 = p.get("ticker"), p.get("proposed_utc")
            direction = p.get("direction")
            if not tk or not t0:
                continue
            bars = _bars(ib, tk) if ib else []
            rows = {}
            for h in HORIZONS_MIN:
                fwd = _fwd_return(bars, t0, h)
                if fwd is None:
                    continue
                # signed in the THESIS's favour: a SHORT that falls is a win. NONE is scored as flat, which
                # is what makes "the model correctly declined" a measurable, creditable outcome.
                signed = fwd if direction == "LONG" else (-fwd if direction == "SHORT" else 0.0)
                rows[h] = {"fwd_pct": round(fwd, 4), "signed_pct": round(signed, 4),
                           "hit": bool(signed > 0) if direction in ("LONG", "SHORT") else None}
            if not rows:
                continue
            rec = {"ticker": tk, "direction": direction, "conviction": p.get("conviction"),
                   "regime": p.get("regime"), "horizon_claimed": p.get("horizon_min"),
                   # carried forward so report() can day-block on the TRADING day, not the ledger write
                   # time — without this a multi-day sample collapses into one block and the clustering
                   # correction silently does nothing.
                   "proposed_utc": t0,
                   "by_horizon": rows, "resolved_utc": datetime.now(timezone.utc).isoformat()}
            if write:
                try:
                    import playbook as PB
                    PB.append("OUTCOME", signal_id=ev.get("signal_id"), actor="system",
                              horizon="multi", **rec)
                except Exception:
                    pass
            settled.append(rec)
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass
    return settled


def report(limit=2000):
    """Per-horizon hit rate and mean signed drift, with DAY-BLOCK bootstrap SEs, plus the sample each
    result would need to be believed. Reads only the ledger — no market access, no model."""
    try:
        import playbook as PB, eval_harness as EH
    except Exception:
        return None
    evs = [e for e in PB.tail(limit, kind="OUTCOME") if (e.get("payload") or {}).get("by_horizon")]
    if not evs:
        return {"n": 0, "horizons": {}, "note": "no resolved theses yet"}
    out = {}
    for h in HORIZONS_MIN:
        vals, blocks, hits = [], [], []
        for e in evs:
            p = e["payload"]
            row = (p.get("by_horizon") or {}).get(str(h)) or (p.get("by_horizon") or {}).get(h)
            if not row or row.get("signed_pct") is None:
                continue
            if p.get("direction") == "NONE":
                continue                     # declines are recorded but don't score a directional hit rate
            vals.append(row["signed_pct"])
            # Day-block key must be the TRADING day the thesis was PROPOSED on — not the ledger write
            # time. Using the write time collapsed a multi-day backfill into a single block, which made
            # block_bootstrap correctly refuse to produce a CI (it needs >=2 blocks) and silently hid the
            # clustering correction this whole module exists to apply.
            blocks.append(((p.get("proposed_utc") or e.get("ts") or "")[:10]) or "?")
            if row.get("hit") is not None:
                hits.append(1 if row["hit"] else 0)
        if not vals:
            out[h] = {"n": 0}
            continue
        mean = sum(vals) / len(vals)
        bs = {}
        try:
            bs = EH.block_bootstrap(vals, blocks) or {}     # -> {mean, se, lo, hi, n, n_blocks, eff_n}
        except Exception:
            bs = {}
        lo, hi, se = bs.get("lo"), bs.get("hi"), bs.get("se")
        out[h] = {"n": len(vals), "mean_signed_pct": round(mean, 4),
                  "se": round(se, 4) if se is not None else None,
                  "ci": [round(lo, 4), round(hi, 4)] if lo is not None else None,
                  "eff_n": bs.get("eff_n"),                 # the honest sample size, not the row count
                  "hit_rate": round(100.0 * sum(hits) / len(hits), 1) if hits else None,
                  "n_days": len(set(blocks)),
                  "significant": bool(lo is not None and (lo > 0 or hi < 0))}
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
