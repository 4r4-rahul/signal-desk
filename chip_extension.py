#!/usr/bin/env python3
"""
chip_extension.py — "is there room left in this move?" PURE MATH, no I/O, no imports beyond math, so it is
unit-testable without importing server (which spawns loop threads). Same contract as chip_probs.py.

chip asked "is the tape strong?" and never asked the two questions that actually killed the 7/21 QQQ 708C:

  1. RUNWAY  — how far is the edge of the recent range in our direction, versus how far T1 actually needs?
               If T1 sits BEYOND the band, the trade needs a regime break, not a scalp. On that trade the
               runway to the 100-min upper band was 0.52 QQQ pts while T1 needed 0.70: the target was
               unreachable at the moment of the fill. Realised max favourable move was +0.64 — inside the
               band, short of T1, exactly as this predicts.
  2. EXTENSION — are we buying the middle of the range or the top of it?

WHY A LONG WINDOW. chip's only anti-chase term was %B off BB(20) on 1-MINUTE bars: a 20-minute window that
re-centres on the very move you are chasing. It read 0.74 ("mid-channel") while the 5-min tape read 0.912
("pinned at the upper band"). A window shorter than the move cannot see the move. Hence horizon_min=100.

HONESTY CONTRACT
  · Every constant below is a PRIOR, not a fit. chip's live sample is n=6 signals, one of them scored.
  · The verdict is driven by RATIO (runway vs the move T1 requires), which is unit-free and needs no vol
    model — a bad σ can never manufacture or suppress a veto.
  · Penalties are additive and capped, so no single term can dominate the tape score.
"""
import math

DEFAULT_HORIZON_MIN = 100      # must exceed the duration of the move being judged; BB(20)/1-min cannot
BAND_K = 2.0                   # band half-width in SDs of the horizon window
MIN_BARS = 30                  # below this the band is noise; degrade rather than guess
SD_FLOOR = 1e-6

# Penalty table (points off the tape score). Additive, capped at PEN_CAP.
PEN_CAP = 22
PEN_POS = ((0.90, 8), (0.80, 5), (0.70, 2))          # how far along the band we are BUYING
PEN_RATIO = ((0.75, 9), (1.00, 6), (1.50, 3))        # runway vs the move T1 needs


def _mean_sd(xs):
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(max(0.0, var))


def bands(closes, k=BAND_K):
    """(lo, mid, hi, sd) over the supplied closes. Caller slices the horizon; this does no windowing."""
    mid, sd = _mean_sd(closes)
    return mid - k * sd, mid, mid + k * sd, sd


def assess(*, spot, closes, req_pts, side="C", horizon_min=DEFAULT_HORIZON_MIN, k=BAND_K):
    """closes: ordered 1-minute closes, MOST RECENT LAST. req_pts: underlying move T1 needs (from
    chip_probs.touch_probs -> req_now_pts). Returns a dict that always carries `penalty` and `verdict`;
    on bad input it degrades to penalty 0 / verdict "unknown" and NEVER blocks an entry on its own."""
    out = {"verdict": "unknown", "penalty": 0, "runway_pts": None, "req_pts": req_pts,
           "ratio": None, "pos": None, "bars": len(closes or []), "horizon_min": horizon_min,
           "degraded": True, "note": None}
    if not closes or len(closes) < MIN_BARS or not spot or spot <= 0:
        out["note"] = f"need {MIN_BARS}+ bars, have {len(closes or [])}"
        return out
    win = closes[-int(horizon_min):]
    lo, mid, hi, sd = bands(win, k)
    width = hi - lo
    if width <= SD_FLOOR or sd <= SD_FLOOR:
        out["note"] = "flat window — no measurable range"
        return out
    is_call = (side or "C").upper() != "P"
    # pos: 0 = buying the far side of the range (fresh), 1 = buying the edge we need to break (extended)
    pos = (spot - lo) / width if is_call else (hi - spot) / width
    runway = (hi - spot) if is_call else (spot - lo)
    out.update(bars=len(win), degraded=False, band_lo=round(lo, 4), band_mid=round(mid, 4),
               band_hi=round(hi, 4), sd=round(sd, 4), pos=round(max(0.0, min(1.0, pos)), 3),
               runway_pts=round(runway, 3))
    pen = 0
    for thr, p in PEN_POS:
        if pos >= thr:
            pen += p
            break
    ratio = None
    if req_pts and req_pts > 0:
        ratio = runway / req_pts
        out["ratio"] = round(ratio, 3)
        for thr, p in PEN_RATIO:
            if ratio < thr:
                pen += p
                break
    out["penalty"] = min(PEN_CAP, pen)
    # verdict is driven by RATIO first (unit-free, no vol model); pos only decides among reachable setups
    if ratio is not None and ratio < 1.0:
        out["verdict"] = "unreachable"          # T1 sits beyond the band — needs a regime break
        out["note"] = f"T1 needs {req_pts:.2f} pts, only {runway:.2f} to the band edge"
    elif pos >= 0.90:
        out["verdict"] = "extended"             # buying the edge itself
    elif ratio is not None and ratio < 1.5:
        out["verdict"] = "tight"
    else:
        out["verdict"] = "room"
    return out
