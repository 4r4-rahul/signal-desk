#!/usr/bin/env python3
"""
structure.py — PRICE-ACTION / market-structure features, pure math on OHLC (no I/O, no deps beyond the
stdlib), so it is unit-testable and cheap to call every lap.

WHY THIS EXISTS. chip's tape is entirely indicator-based (EMA/VWAP/RSI/Bollinger/momentum). It has no
notion of STRUCTURE — swings, higher-lows, displacement, premium/discount, imbalance — which is the lens
a discretionary trader actually reads:

    "Displacement higher shows intent to shift structure, but it pushes us into where sellers can trade
     us lower, so buyers must defend the retest for a higher low to continue."

None of that is in the tape today. This module computes those features so they are CAPTURED on every
scalp's entry-feature vector. It does NOT gate entries — the desk's rule is measure first: log the
feature, then over the month test whether "bought a displacement into premium with no defended higher
low" actually predicts chip's losers. If it does, THEN it earns a place in the gate.

Everything fail-softs to None so a thin/degenerate tape never throws.

Vocabulary, defined so the numbers are unambiguous:
  · swing high/low — a fractal pivot: a high (low) that is the max (min) of the k bars on EACH side.
    The last k bars cannot be confirmed pivots (need future bars) — that is correct, not a gap.
  · structure — the sequence of the last swings: higher-highs+higher-lows = up, lower-lows+lower-highs
    = down, else range/transition.
  · BOS (break of structure) — the latest close took out the most recent confirmed swing high (bull) or
    low (bear): the market "continuing/committing" in that direction.
  · displacement — an impulsive leg: the largest close-to-close move over a short window measured in ATR.
    >= DISP_ATR is a real displacement (intent), not noise.
  · FVG (fair-value gap / imbalance) — a 3-bar gap: bar[i-2].high < bar[i].low (bull) or
    bar[i-2].low > bar[i].high (bear). The unfilled void a displacement leaves behind.
  · premium/discount — where price sits in the last confirmed swing range: 0.0 = at the low (discount),
    1.0 = at the high (premium). "Pushed into where sellers can trade us lower" == high premium.
"""

PIVOT_K = 2          # bars on each side to confirm a fractal swing (2 = responsive; 3 = smoother)
DISP_ATR = 1.6       # a leg >= this many ATR over DISP_WIN bars counts as a displacement
DISP_WIN = 3         # window (bars) for the impulsive-leg measure
ATR_N = 14           # ATR lookback


def _atr(high, low, close, n=ATR_N):
    if len(close) < n + 1:
        return None
    trs = []
    for i in range(1, len(close)):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def _pivots(high, low, k=PIVOT_K):
    """Confirmed swing highs/lows as (index, price). The last k bars are excluded (unconfirmable)."""
    hi, lo = [], []
    for i in range(k, len(high) - k):
        if high[i] == max(high[i - k:i + k + 1]) and high[i] > max(high[i - k:i] + high[i + 1:i + k + 1]):
            hi.append((i, high[i]))
        if low[i] == min(low[i - k:i + k + 1]) and low[i] < min(low[i - k:i] + low[i + 1:i + k + 1]):
            lo.append((i, low[i]))
    return hi, lo


def features(high, low, close):
    """The structure feature vector for one bar series (oldest -> newest). All fail-soft to None."""
    out = {
        "structure": None,          # 'up' | 'down' | 'range'
        "bos": None,                # 'bull' | 'bear' | None  (latest break of structure)
        "higher_low": None,         # True if the last swing low > the prior swing low
        "lower_high": None,         # True if the last swing high < the prior swing high
        "displacement": None,       # 'bull' | 'bear' | None  (impulsive leg present + its direction)
        "disp_atr": None,           # magnitude of the recent leg in ATR (signed)
        "fvg": None,                # 'bull' | 'bear' | None  (most recent unfilled 3-bar gap)
        "in_fvg": None,             # True if price currently sits inside that gap
        "premium_pct": None,        # 0=at swing low (discount) .. 1=at swing high (premium)
        "dist_to_hi_pct": None,     # % up to the nearest swing high (runway to supply)
        "dist_to_lo_pct": None,     # % down to the nearest swing low (runway to demand)
        "n_swings_hi": 0, "n_swings_lo": 0,
    }
    try:
        n = len(close)
        if n < 2 * PIVOT_K + 5 or len(high) != n or len(low) != n:
            return out
        px = close[-1]
        hi, lo = _pivots(high, low)
        out["n_swings_hi"], out["n_swings_lo"] = len(hi), len(lo)

        # structure + higher-low / lower-high from the last two swings of each kind
        if len(hi) >= 2:
            out["lower_high"] = hi[-1][1] < hi[-2][1]
        if len(lo) >= 2:
            out["higher_low"] = lo[-1][1] > lo[-2][1]
        if len(hi) >= 2 and len(lo) >= 2:
            hh = hi[-1][1] > hi[-2][1]
            hl = lo[-1][1] > lo[-2][1]
            ll = lo[-1][1] < lo[-2][1]
            lh = hi[-1][1] < hi[-2][1]
            out["structure"] = "up" if (hh and hl) else "down" if (ll and lh) else "range"

        # BOS: did the latest close break the most recent CONFIRMED swing?
        last_hi = hi[-1][1] if hi else None
        last_lo = lo[-1][1] if lo else None
        if last_hi is not None and px > last_hi:
            out["bos"] = "bull"
        elif last_lo is not None and px < last_lo:
            out["bos"] = "bear"

        # premium/discount + runway, from the enclosing swing range
        if last_hi is not None and last_lo is not None and last_hi > last_lo:
            out["premium_pct"] = round((px - last_lo) / (last_hi - last_lo), 3)
            out["dist_to_hi_pct"] = round(max(0.0, (last_hi - px)) / px * 100, 3) if px else None
            out["dist_to_lo_pct"] = round(max(0.0, (px - last_lo)) / px * 100, 3) if px else None

        # displacement: the strongest close-to-close leg over the last DISP_WIN bars, in ATR
        atr = _atr(high, low, close)
        if atr and atr > 0 and n > DISP_WIN:
            leg = close[-1] - close[-1 - DISP_WIN]
            out["disp_atr"] = round(leg / atr, 2)
            if abs(leg) / atr >= DISP_ATR:
                out["displacement"] = "bull" if leg > 0 else "bear"

        # FVG: most recent unfilled 3-bar imbalance, and whether price is inside it
        for i in range(n - 1, 1, -1):
            if high[i - 2] < low[i]:                       # bullish gap between bar i-2 high and bar i low
                if px >= high[i - 2]:                      # still unfilled (price above the gap top)
                    out["fvg"] = "bull"; out["in_fvg"] = bool(low[i] >= px >= high[i - 2]); break
            if low[i - 2] > high[i]:                        # bearish gap
                if px <= low[i - 2]:
                    out["fvg"] = "bear"; out["in_fvg"] = bool(high[i] <= px <= low[i - 2]); break
    except Exception:
        pass
    return out


def features_from_bars(bars):
    """Convenience: pull high/low/close off IBKR bar objects and compute."""
    try:
        return features([float(b.high) for b in bars], [float(b.low) for b in bars],
                        [float(b.close) for b in bars])
    except Exception:
        return features([], [], [])
