#!/usr/bin/env python3
"""
chip_strike.py — which strike on the ladder. PURE MATH, no I/O, no imports beyond math. Same contract as
chip_probs.py / chip_extension.py, so it is unit-testable without importing server.

VETO-ONLY MODE, deliberately. The SME finding that shapes this module is a NEGATIVE one:

    Under a %-of-premium stop, strike choice is nearly a no-op for expectancy.

That is provable, not empirical. t1_pct and stop_pct are both percentages of the SAME premium, so the
underlying move each needs is dS ~ (target% * P)/delta and therefore

    dS_T1 / dS_stop  =  t1_pct / stop_pct          <- delta and premium CANCEL

A cheaper strike does reach T1 on a smaller move — and reaches the stop on a proportionally smaller move.
Measured across the real 7/21 QQQ ladder the gambler's-ruin race is flat: 0.587 (705C, d0.79) -> 0.597
(708C, d0.54) -> 0.556 (712C, d0.16). Ranking strikes on expected value would be fitting noise: the spread
of E[%] across a 4x range of premium is ~1.1pp against a per-cell standard error of ~0.2pp.

What does NOT cancel, and is therefore the entire value of this module:
  · relative spread — a fixed % of premium, so it punishes cheap strikes monotonically
  · dispersion — outcome sigma rises with leverage, so E/sigma falls monotonically going OTM
  · the theta fuse — the T1 barrier flees fastest on the cheapest strikes

So: no EV ranking, no optimiser. Apply hard vetoes, then take the LOWEST-LEVERAGE (deepest ITM) survivor.
That rule is sigma-free — it needs no vol model, so a bad IV can never talk it into a lottery ticket — and
monotone, so it cannot flip between laps on quote jitter. Expected value of the tie-break is ~+0.02R; the
value of the VETOES is avoiding the -5 to -17pp of E[%] that the far-OTM rungs destroy in spread alone.

Ranked honestly this is the FOURTH lever, behind entry quality (~+0.39R), stop geometry (3x dispersion
collapse) and DTE. Do not let it masquerade as an expectancy fix.
"""
import math

DELTA_MIN, DELTA_MAX = 0.25, 0.85
MIN_PREMIUM_ETF, MIN_PREMIUM_SINGLE = 0.40, 0.75
ETFS = {"SPY", "QQQ", "IWM", "DIA"}
FUSE_HOLD_MULT = 2.0            # a contract whose DECAY alone trips the stop inside 2x the intended hold
LADDER_N = 2                    # +/- N increments around ATM => 2N+1 rungs


def _fin(x):
    try:
        f = float(x)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def ladder_strikes(spot, inc, n=LADDER_N):
    """The 2n+1 strikes bracketing ATM, in increment units. O(1), cache-friendly against the quote pool."""
    if not spot or not inc or inc <= 0:
        return []
    atm = round(spot / inc) * inc
    out = []
    for i in range(-n, n + 1):
        k = atm + i * inc
        if k > 0:
            out.append(round(k, 4))
    return out


def _veto(q, *, spot, mech, ticker, hold_min, equity, max_risk_pct):
    """First veto wins and is reported. Order is cheapest-and-most-certain first."""
    mid, ask = _fin(q.get("mid")), _fin(q.get("ask"))
    delta, spread = _fin(q.get("delta")), _fin(q.get("spread_pct"))
    px = ask or mid
    if px is None or delta is None:
        return "no quote"
    smax = mech.get("spread_max")
    if spread is not None and smax is not None and spread > smax:
        return f"spread {spread:.1f}%>{smax}%"
    floor = MIN_PREMIUM_ETF if (ticker or "").upper() in ETFS else MIN_PREMIUM_SINGLE
    if px < floor:
        return f"premium ${px:.2f}<${floor:.2f}"      # a penny tick is percentage points of a cheap contract
    ad = abs(delta)
    if ad < DELTA_MIN:
        return f"delta {ad:.2f}<{DELTA_MIN}"           # MC turns sharply negative below here; pure cost
    if ad > DELTA_MAX:
        return f"delta {ad:.2f}>{DELTA_MAX}"           # paying for intrinsic a scalp does not use
    fuse = q.get("fuse_min")
    fstate = q.get("fuse_state")
    if fstate != "never":                              # "never" = stop sits below intrinsic; always fine
        if fuse is None:
            return "fuse unknown"                      # FAIL CLOSED: it is a safety filter
        if hold_min and fuse < FUSE_HOLD_MULT * hold_min:
            return f"theta fuse {int(fuse)}m<{int(FUSE_HOLD_MULT*hold_min)}m"
    # NB: the 1% risk cap is deliberately NOT a veto here. It vetoes ITM-first (highest premium first), so
    # it inverted this module's whole objective: at $9.5k it eliminated 706/707/708/709 and left 710 -- the
    # HIGHEST-leverage rung -- as the "lowest-leverage survivor". Risk belongs to SIZE, not to strike.
    # See size_for() below; the caller refuses the trade when even one contract breaches the cap.
    return None


def size_for(px, stop_pct, equity, max_risk_pct=1.0):
    """Contracts affordable under the risk cap, and the $ risk of one. Returns (qty, risk_per_contract).
    qty == 0 means NOT TRADEABLE at this equity — the caller must refuse, never walk OTM to make it fit.
    (QQQ 1DTE ATM at a 28% stop is ~$125/contract, which breaches a $95 cap on ~95% of entries: that is a
    real 'this instrument is too big for this account' signal and must not be silently routed around.)"""
    if not px or not stop_pct:
        return 0, None
    risk = stop_pct / 100.0 * px * 100.0
    if not equity:
        return 1, round(risk, 2)              # no equity known -> don't fabricate a limit
    return int(equity * (max_risk_pct / 100.0) // risk), round(risk, 2)


def select(*, quotes, spot, mech, ticker=None, hold_min=60, equity=None, max_risk_pct=1.0):
    """quotes: list of dicts {strike, mid, ask, bid, delta, spread_pct, fuse_min, fuse_state}.
    Returns {chosen, atm, rungs[], degraded, note}. NEVER raises and never blocks an emit: if fewer than
    two rungs survive it reports degraded and leaves the caller on its existing ATM behaviour."""
    out = {"chosen": None, "atm": None, "rungs": [], "degraded": True, "note": None,
           "objective": "lowest-leverage survivor (sigma-free)"}
    if not quotes or not spot:
        out["note"] = "no quotes"
        return out
    inc = mech.get("strike_inc", 1.0)
    out["atm"] = round(spot / inc) * inc if inc else None
    rungs = []
    for q in quotes:
        k = _fin(q.get("strike"))
        px = _fin(q.get("ask")) or _fin(q.get("mid"))
        delta = _fin(q.get("delta"))
        if k is None:
            continue
        # leverage: elasticity of the option to the underlying. Lower = safer, and it is the whole ranking.
        lam = (abs(delta) * spot / px) if (delta is not None and px) else None
        veto = _veto(q, spot=spot, mech=mech, ticker=ticker, hold_min=hold_min,
                     equity=equity, max_risk_pct=max_risk_pct)
        rungs.append({"strike": k, "delta": delta, "premium": px,
                      "spread_pct": _fin(q.get("spread_pct")), "lam": round(lam, 1) if lam else None,
                      "fuse_min": q.get("fuse_min"), "fuse_state": q.get("fuse_state"),
                      "veto": veto, "chosen": False})
    rungs.sort(key=lambda r: r["strike"])
    out["rungs"] = rungs
    ok = [r for r in rungs if not r["veto"] and r["lam"] is not None]
    if len(ok) < 2:
        out["note"] = "ladder unavailable — ATM fallback" if not ok else "only one rung survived"
        if ok:
            ok[0]["chosen"] = True
            out["chosen"] = ok[0]
        return out
    best = min(ok, key=lambda r: r["lam"])       # lowest leverage that clears every veto
    best["chosen"] = True
    out.update(chosen=best, degraded=False)
    atm_r = min(rungs, key=lambda r: abs(r["strike"] - (out["atm"] or spot)))
    if best is not atm_r:
        out["note"] = (f"{best['strike']:g} over ATM {atm_r['strike']:g}: leverage "
                       f"{best['lam']} vs {atm_r['lam']}" + (f" (ATM {atm_r['veto']})" if atm_r["veto"] else ""))
    return out
