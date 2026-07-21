#!/usr/bin/env python3
"""
chip_probs.py — touch-probabilities for a live chip scalp. PURE MATH, no I/O, no imports beyond math,
so it is unit-testable without importing server (which spawns loop threads).

The point of this module: on a 0DTE the option's premium decays, so the UNDERLYING move required to reach
a premium target GROWS with time — the barrier flees. A static barrier badly overstates P(target) and
understates P(stop). That drift is why a card can read "thesis intact" while T1 is quietly unreachable.

HONESTY CONTRACT
  · The ACTION must be driven by the σ-FREE quantities (t1_first via gambler's ruin, and the theta fuse).
    Both are independent of the volatility input, so a bad σ can never manufacture a CUT.
  · The σ-dependent ladder probabilities (t1/t2/t3/stop) are DISPLAY-ONLY.
  · Every approximation here is deliberately CONSERVATIVE (biases P(target) down): the delta/gamma quadratic
    overstates the move needed for large ΔS, and the chord fit to a convex barrier is an upper bound on the
    barrier ⇒ a lower bound on P.
Assumptions (surface these in the UI): driftless · lognormal · constant σ · no skew · √t decay · ignores
that half the position comes off at T1.
"""
import math

TRADING_HOURS_YR = 1638.0          # 252 sessions × 6.5h
CALENDAR_HOURS_YR = 8760.0         # 365 × 24
# IBKR quotes impliedVol CALENDAR-annualised, but tte_yr below is in TRADING-hour years. Feeding one into
# the other overstated σ√T by √(8760/1638) = 2.31×: the real QQQ 708C (S 708.38, IV .244, 278 min) priced
# at $3.86 with δ .519 / γ .0433 against an observed 1.79 / .540 / .0991. Rescaled, Black-Scholes reproduces
# all three to 3dp. NB: SIGMA_DEFAULT below was already authored in trading-time (0.13 ÷ .43242 = 0.30
# calendar, a sane QQQ 0DTE) — so ONLY the iv branch needs this, and the mark-inversion branch is
# self-consistent (it inverts the same tte_yr it was built from).
SIG_CAL_TO_TRADING = math.sqrt(TRADING_HOURS_YR / CALENDAR_HOURS_YR)   # 0.43242
SIG_MIN, SIG_MAX = 0.05, 3.00
P_MIN, P_MAX = 0.01, 0.99
DELTA_MIN = 0.05                   # below this it's a lotto, not a scalp — refuse to model
TTE_FLOOR_MIN = 2.0
FAR_MOVE_CAP = 0.25                # |ΔS|/S beyond this ⇒ effectively unreachable
EXP_CAP = 50.0                     # math.exp guard — an overflow would emit NaN and blank the feed
RACE_WINDOW_MIN = 30.0
SIGMA_DEFAULT = {"SPY": 0.11, "QQQ": 0.13, "NVDA": 0.45, "TSLA": 0.55, "AAPL": 0.25}
SIGMA_FALLBACK = 0.30


def _N(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _cap(x, lim=EXP_CAP):
    return max(-lim, min(lim, x))


def _clampp(p):
    return round(max(P_MIN, min(P_MAX, p)), 3)


def _ds_for(dp, delta, gamma):
    """Underlying move required for a premium change `dp`, via 2nd-order Taylor.
    ΔP = δ·ΔS + ½γ·ΔS²  ⇒  ΔS = (−δ + √(δ² + 2γΔP))/γ.  Falls back to ΔP/δ when γ is unusable."""
    d = abs(delta or 0.0)
    if d < DELTA_MIN:
        return None
    g = gamma if (gamma and gamma > 1e-9) else None
    if g is None:
        return dp / d
    disc = d * d + 2.0 * g * dp
    if disc < 0:                      # premium target below zero-value floor
        return None
    ds = (-d + math.sqrt(disc)) / g
    d_star = (1.0 - d) / g            # delta saturates at 1.0 — linear beyond
    if ds > d_star > 0:
        dp_at = d * d_star + 0.5 * g * d_star * d_star
        ds = d_star + (dp - dp_at)
    return ds


def _touch(a, mu_T, sig_sqrt_T, up=True):
    """First-passage to a LINEAR (drifting) log-barrier — Doob: a moving boundary under driftless BM is a
    fixed level under drift. Reduces to the classic 2·N(−|a|/σ√T) when the barrier is static."""
    if sig_sqrt_T <= 0:
        return P_MIN
    m = abs(a)
    if up:
        z1, z2 = (-m + mu_T) / sig_sqrt_T, (-m - mu_T) / sig_sqrt_T
        ex = _cap(2.0 * mu_T * m / (sig_sqrt_T ** 2))
    else:
        z1, z2 = (-m - mu_T) / sig_sqrt_T, (-m + mu_T) / sig_sqrt_T
        ex = _cap(-2.0 * mu_T * m / (sig_sqrt_T ** 2))
    return _N(z1) + math.exp(ex) * _N(z2)


def overnight_factor(mins_to_close, dte):
    """sqrt(calendar hours / trading hours) to expiry — the term that makes the calendar->trading-time
    conversion correct when the window CONTAINS AN OVERNIGHT. Derived from mins_to_close and dte alone, so
    this module stays clock-free and pure.

    This is exactly 1.0 for a same-session 0DTE, which is why the bare SIG_CAL_TO_TRADING constant looked
    right when it was validated against a 0DTE quote — and then went silently wrong the day chip's default
    moved to 1DTE. At 1DTE/15:20 the missing factor is 1.855, i.e. sigma was understated by ~46%.
    (Approximation: ignores weekends, so a Friday 1DTE over-states calendar time. Bounded and conservative
    in the direction of a WIDER sigma, and the action path is sigma-free regardless.)"""
    d = int(dte or 0)
    if d <= 0 or not mins_to_close or mins_to_close <= 0:
        return 1.0
    m0 = max(0.0, float(mins_to_close) - 390.0 * d)      # minutes left in TODAY's session
    cal = m0 + 1440.0 * d                                # wall-clock minutes to the expiry bell
    return math.sqrt(cal / float(mins_to_close))


def _sigma(iv, mark, spot, strike, tte_yr, ticker, on_factor=1.0):
    """implied → mark-inverted (Brenner–Subrahmanyam, ATM only) → per-ticker default."""
    if iv and SIG_MIN <= iv <= SIG_MAX:
        # calendar-annualised (IBKR) -> trading-time, INCLUDING the overnight term
        return float(iv) * SIG_CAL_TO_TRADING * on_factor, "iv", False
    # Brenner-Subrahmanyam assumes the premium is ~ALL extrinsic, so the band must be judged against the
    # PREMIUM, not the spot. The old `|S-K|/S < 1.5%` was $11 on SPY: a $2.28 ITM call priced ~$3 is 76%
    # intrinsic and inverts to a sigma ~1.6x too high. abs(spot-strike) bounds intrinsic without needing cp.
    if mark and spot and tte_yr > 0 and strike and abs(spot - strike) <= 0.05 * mark:
        s = mark / (0.3989 * spot * math.sqrt(tte_yr))
        if SIG_MIN <= s <= SIG_MAX:
            return s, "mark", False
    return SIGMA_DEFAULT.get((ticker or "").upper(), SIGMA_FALLBACK), "default", True


def touch_probs(*, spot, strike, cp, mark, entry, delta, gamma, iv,
                t1_pct, t2_pct, t3_pct, cur_stop_pct, stop_pct,
                mins_to_close, dte, ticker=None, hit=(False, False, False), asof_s=0, hold_min=None):
    """Returns the probs dict, or None when the inputs can't support an honest estimate."""
    if not (spot and mark and entry and mins_to_close is not None):
        return None
    if mins_to_close <= TTE_FLOOR_MIN:
        return {"method": "expired", "stale": True, "tte_min": round(mins_to_close or 0)}
    if not delta or abs(delta) < DELTA_MIN:
        return {"method": "nodelta", "stale": True, "tte_min": round(mins_to_close)}
    is_call = (cp or "C").upper() != "P"
    # TWO DIFFERENT CLOCKS, and conflating them is wrong for anything past 0DTE:
    #   T_dec = minutes of DECAY left (to expiry)      -> prices the barrier's flight
    #   T_dif = minutes we actually intend to HOLD     -> the first-passage horizon
    # On a 0DTE these nearly coincide (278 vs 278). On a 1DTE the old code diffused over 668 min against a
    # 45-min scalp and duly "found" the stop: it reported P(stop) 0.92 for a 1DTE vs 0.90 for a 0DTE, i.e.
    # that doubling the stop's distance in QQQ points changed nothing. Over the real 60-min hold it is
    # 0.23 vs 0.62. Same barrier, honest horizon.
    T_dec = float(mins_to_close)
    T_dif = min(float(hold_min), T_dec) if hold_min else T_dec
    tte_yr = (T_dec / 60.0) / TRADING_HOURS_YR         # decay clock -> also the σ-inversion basis
    dif_yr = (T_dif / 60.0) / TRADING_HOURS_YR
    sig, sig_src, sig_stale = _sigma(iv, mark, spot, strike, tte_yr, ticker,
                                     overnight_factor(T_dec, dte))
    # SELF-CHECK: the mark-inversion branch is internally self-consistent, so when both are available a
    # large disagreement means the IV conversion is wrong. This is precisely what would have caught the
    # 1DTE bug on its first card instead of a review. Surfaced, never acted on.
    sig_check = None
    if sig_src == "iv" and mark and spot and strike and tte_yr > 0 and abs(spot - strike) <= 0.05 * mark:
        s_mark = mark / (0.3989 * spot * math.sqrt(tte_yr))
        if SIG_MIN <= s_mark <= SIG_MAX and sig > 0:
            r = s_mark / sig
            if r > 1.30 or r < 0.77:
                sig_check = round(r, 2)
    sst = sig * math.sqrt(dif_yr)                      # diffusion over the HOLD, not to the bell
    if sst <= 0:
        return {"method": "nosigma", "stale": True, "tte_min": round(T_dec)}
    intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
    extrinsic = max(0.0, mark - intrinsic)
    at = lambda p: entry * (1.0 + (p or 0) / 100.0)     # premium level for a %-of-entry target

    def _mark_at(t):                                    # extrinsic decays ≈ √(remaining/total)
        if T_dec <= 0:
            return intrinsic
        return intrinsic + extrinsic * math.sqrt(max(0.0, (T_dec - t) / T_dec))

    def _p_target(target_px, up):
        """Chord-fit the (convex) drifting log-barrier, then one closed-form first-passage."""
        ds0 = _ds_for(target_px - mark, delta, gamma)
        dsT = _ds_for(target_px - _mark_at(T_dif), delta, gamma)   # barrier where the HOLD ends
        if ds0 is None or dsT is None:
            return None, None, None
        sgn = 1.0 if is_call else -1.0
        s0, sT = spot + sgn * ds0, spot + sgn * dsT
        if s0 <= 0 or sT <= 0:
            return None, None, None
        if max(abs(s0 - spot), abs(sT - spot)) / spot > FAR_MOVE_CAP:
            return P_MIN, ds0, dsT
        a, bT = math.log(s0 / spot), math.log(sT / spot)
        mu_T = -(bT - a)                                # chord slope, expressed as drift over the window
        return _touch(a, mu_T, sst, up=(a > 0)), ds0, dsT

    p1, req_now, req_eod = _p_target(at(t1_pct), True)
    p2, _, _ = _p_target(at(t2_pct), True)
    p3, _, _ = _p_target(at(t3_pct), True)
    stop_px = at(cur_stop_pct)
    ps, _, _ = _p_target(stop_px, False)
    # ── σ-FREE #1: the theta fuse — when decay ALONE reaches the stop, at zero movement ──
    # fuse_state disambiguates what a bare `fuse=None` used to conflate: "never" (stop sits BELOW intrinsic,
    # so decay ALONE can never reach it — a strong ITM positive) vs "unknown" (can't compute). A fuse veto
    # must pass on "never" and fail CLOSED on "unknown"; the UI must not render "never" as missing data.
    fuse, fuse_state = None, "unknown"
    if extrinsic <= 1e-9:
        fuse_state = "never"                       # no extrinsic left to burn
    else:
        r = (stop_px - intrinsic) / extrinsic
        if r <= 0.0:
            fuse_state = "never"                   # premium floors at intrinsic, which is above the stop
        elif r >= 1.0:
            fuse, fuse_state = 0, "now"            # stop already at/above the mark
        else:
            fuse, fuse_state = int(round(T_dec * (1.0 - r * r))), "min"
    # ── σ-FREE #2: gambler's ruin — does T1 come BEFORE the stop? (independent of σ and T) ──
    t1_first = None
    if not hit[0]:
        tm = min(RACE_WINDOW_MIN, T_dif, (fuse / 2.0) if fuse else RACE_WINDOW_MIN) / 2.0
        mk = _mark_at(tm)
        du = _ds_for(at(t1_pct) - mk, delta, gamma)
        dd = _ds_for(stop_px - mk, delta, gamma)
        if du is not None and dd is not None:
            a_up, m_dn = abs(du), abs(dd)
            if (a_up + m_dn) > 0:
                t1_first = _clampp(m_dn / (a_up + m_dn))
    out = {"t1": 1.0 if hit[0] else (_clampp(p1) if p1 is not None else None),
           "t2": 1.0 if hit[1] else (_clampp(p2) if p2 is not None else None),
           "t3": 1.0 if hit[2] else (_clampp(p3) if p3 is not None else None),
           "stop": _clampp(ps) if ps is not None else None,
           "t1_first": t1_first,
           "sigma": round(sig, 4), "sigma_src": sig_src, "sigma_disagree": sig_check,
           "tte_min": int(round(T_dec)), "horizon_min": int(round(T_dif)), "dte": dte,
           "req_now_pts": round(req_now, 2) if req_now is not None else None,
           "req_eod_pts": round(req_eod, 2) if req_eod is not None else None,
           "stop_theta_min": fuse, "fuse_state": fuse_state,
           "sigma_conv": "cal2trd" if sig_src == "iv" else sig_src,
           "method": "gbm-touch-linbarrier" if (gamma and gamma > 1e-9) else "delta-only",
           "stale": bool(sig_stale or (asof_s or 0) > 90), "asof_s": int(asof_s or 0)}
    for k in ("t2", "t3"):                              # nested by construction — never let noise invert it
        if out[k] is not None and out["t1"] is not None and out[k] > out["t1"]:
            out[k] = out["t1"]
    if out["t3"] is not None and out["t2"] is not None and out["t3"] > out["t2"]:
        out["t3"] = out["t2"]
    return out
