#!/usr/bin/env python3
"""
exec_plan.py — turn a target ladder into an EXECUTABLE one, in whole contracts.

THE BUG THIS EXISTS FOR: the desk's scale-out is "1/2 at T1, 1/4 at T2, 1/4 runner". That is only
executable when the position is a multiple of 4. At $50k the book sizes SPY 1DTE ATM to 7 contracts and
QQQ to 3 — so "half out at T1" means selling 3.5 and 1.5 contracts, which cannot be done. Every chip trade
scored so far was graded against a plan that could not have been executed.

This resolves the ladder into whole contracts, or says plainly that the position is too small to have one:

    qty 1  ->  all out at T1. There is no ladder. Do not pretend there is.
    qty 2  ->  1 at T1, 1 runner (no T2 leg — a 2-lot cannot express three exits)
    qty 3  ->  1 / 1 / 1 thirds
    qty>=4 ->  round(1/2), round(1/4), remainder — the intended shape, with the remainder on the RUNNER
               so rounding never sells more than we hold.

WHY THE REMAINDER GOES TO THE RUNNER: the alternative is selling a contract we do not own, which is not a
rounding preference, it is an impossible order. Everything else follows from that.

The point is not elegance. It is that the plan we SCORE has to be the plan we could TRADE, or the whole
measurement is of a strategy nobody can run.
"""

MIN_LADDER_QTY = 4          # below this the intended 1/2-1/4-1/4 shape cannot be expressed at all


def resolve_ladder(qty, t1_pct, t2_pct, t3_pct, stop_pct):
    """Return the executable exit plan for `qty` whole contracts.

    {legs: [{at, pct_of_position, qty, kind}], runner_qty, shape, degraded, note}
    `shape` names what we can actually do, so the record never claims a ladder it did not run."""
    q = int(qty or 0)
    if q < 1:
        return {"legs": [], "runner_qty": 0, "shape": "none", "degraded": True,
                "note": "no position"}
    if q == 1:
        return {"legs": [{"at": "T1", "target_pct": t1_pct, "qty": 1, "kind": "all_out"}],
                "runner_qty": 0, "shape": "all_out_t1", "degraded": True,
                "note": "1 contract — no ladder is possible; this is all-out at T1, "
                        "so T2/T3 are untested on this trade"}
    if q == 2:
        return {"legs": [{"at": "T1", "target_pct": t1_pct, "qty": 1, "kind": "scale"},
                         {"at": "T3", "target_pct": t3_pct, "qty": 1, "kind": "runner"}],
                "runner_qty": 1, "shape": "half_runner", "degraded": True,
                "note": "2 contracts — T2 leg cannot be expressed; 1 at T1, 1 runner"}
    if q == 3:
        return {"legs": [{"at": "T1", "target_pct": t1_pct, "qty": 1, "kind": "scale"},
                         {"at": "T2", "target_pct": t2_pct, "qty": 1, "kind": "scale"},
                         {"at": "T3", "target_pct": t3_pct, "qty": 1, "kind": "runner"}],
                "runner_qty": 1, "shape": "thirds", "degraded": True,
                "note": "3 contracts — thirds, not 1/2-1/4-1/4"}
    n1 = max(1, round(q * 0.50))
    n2 = max(1, round(q * 0.25))
    n3 = q - n1 - n2                       # remainder to the RUNNER: never sell what we do not hold
    if n3 < 1:                             # pathological rounding -> give the runner a contract back
        n2 = max(1, n2 - 1)
        n3 = q - n1 - n2
    return {"legs": [{"at": "T1", "target_pct": t1_pct, "qty": n1, "kind": "scale"},
                     {"at": "T2", "target_pct": t2_pct, "qty": n2, "kind": "scale"},
                     {"at": "T3", "target_pct": t3_pct, "qty": n3, "kind": "runner"}],
            "runner_qty": n3, "shape": "ladder", "degraded": False,
            "note": f"{n1}/{n2}/{n3} of {q} — intended shape, whole contracts"}


def min_qty_for_full_ladder():
    return MIN_LADDER_QTY


def tests_t2_t3(plan):
    """Does this position actually TEST the T2 and T3 legs? A book full of 1-lots can never learn
    whether its own T2/T3 targets are any good — the data to answer that is simply never generated."""
    ats = {l["at"] for l in (plan.get("legs") or [])}
    return {"t1": "T1" in ats, "t2": "T2" in ats, "t3": "T3" in ats}


def coverage(plans):
    """Across many positions: what fraction of the book actually exercised each leg. This is the number
    that says whether the T2/T3 targets are being MEASURED or merely declared."""
    if not plans:
        return {"n": 0}
    n = len(plans)
    c = {"t1": 0, "t2": 0, "t3": 0}
    for p in plans:
        t = tests_t2_t3(p)
        for k in c:
            c[k] += 1 if t[k] else 0
    return {"n": n, "t1_pct": round(100.0 * c["t1"] / n, 1), "t2_pct": round(100.0 * c["t2"] / n, 1),
            "t3_pct": round(100.0 * c["t3"] / n, 1),
            "full_ladder_pct": round(100.0 * sum(1 for p in plans if not p.get("degraded")) / n, 1)}
