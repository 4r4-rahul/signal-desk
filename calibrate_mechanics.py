#!/usr/bin/env python3
"""
calibrate_mechanics.py — MEASURE the per-ticker stop/target mechanics instead of asserting them.

THE PROBLEM THIS EXISTS TO FIX. chip trades 10 tickers. Two of them — SPY and QQQ — have stop/t1
numbers backed by three independent measurements. The other eight (IWM, NVDA, AAPL, GOOGL, TSLA, META,
AMZN, MSFT) carry hand-set numbers scaled by judgement ("single names are wider, so widen the stop"),
and chip_mechanics.json — the nightly override that is supposed to replace them — has never existed.
They went live on 2026-07-23 on numbers nobody measured.

WHY THEY COULD NOT SIMPLY BE MEASURED FROM STORED QUOTES. The desk's own `path` table holds real option
quotes, and that is the gold standard, but it holds ONE day for each of these eight. The measurement
convention here is a day-block bootstrap, where effective n is independent DAYS, not ticks — with one
block there is no interval to compute. Waiting for 30+ sessions of live option quotes is the rigorous
path and it is still the right long-run answer; this module is what can be known TODAY.

THE METHOD. Real underlying price paths from IBKR, with an analytic option->underlying barrier map:

    ATM premium  P ≈ 0.3989 · S · σ · √T          (Black-Scholes ATM, 0.3989 = 1/√(2π))
    option move  s/100 = Δ·ΔS / P
    =>  ΔS/S = (s/100) · (0.3989/Δ) · σ · √T

so a premium-% stop becomes an underlying-% barrier. Normalised by the horizon's own σ the IV cancels
entirely — k = (s/100)·(0.3989/Δ)·√(T/w) — which is why this can be compared across tickers of wildly
different volatility without pretending they are the same instrument.

WHAT IS REAL AND WHAT IS MODELLED, stated plainly because the difference is the whole value:
  · REAL      — the price paths, every touch, the order of touches, the day blocks.
  · MODELLED  — the map from premium-% to underlying-%. It assumes ATM, constant Δ over the hold, and
                no vol path. It therefore does NOT capture theta bleed, IV crush, or spread widening.
  · CONSEQUENCE — these numbers are a RANKING of candidate stop/t1 settings under a common assumption,
                not a P&L forecast. Use them to choose between 26 and 32; do not quote the R as an
                expected return. Real stored quotes supersede this the moment there are enough days.

Run:  python3 calibrate_mechanics.py                  measure all unmeasured tickers, print only
      python3 calibrate_mechanics.py --write          also write chip_mechanics.json
      python3 calibrate_mechanics.py --tickers NVDA,AAPL --days 60
"""
import argparse
import json
import math
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import market as MK

OUT = Path(__file__).parent / "chip_mechanics.json"
CLIENT_ID = 88                   # distinct from monitor 80/81, chip 82, ladder 84
ATM_DELTA = 0.50                 # chip scalps ATM; that is the contract this map is written for
BARS_PER_HOLD = 4                # 5-min bars in the 20-minute scalp window
SQRT_2PI_INV = 0.3989

# The candidate grid. t1 is expressed as a MULTIPLE of the stop so the asymmetry being tested is
# explicit — the SPY/QQQ result was "t1 -> 1.0x stop", and the single names were left on the old
# asymmetric ratios purely because they were not live yet.
STOP_GRID = (22, 26, 30, 34, 38)
T1_RATIOS = (0.8, 1.0, 1.2, 1.4)
DEFAULT_TICKERS = ("IWM", "NVDA", "AAPL", "GOOGL", "TSLA", "META", "AMZN", "MSFT")


def _sessions(bars):
    """Group bars into trading days. The DAY is the bootstrap block — consecutive 5-min bars inside one
    session are ~36-fold overlapping, so treating ticks as independent overstates confidence by roughly
    sqrt(ticks/days). This is the same convention eval_harness uses; keeping it identical is what lets
    these numbers sit beside the SPY/QQQ ones without a units mismatch."""
    days = defaultdict(list)
    for b in bars:
        d = b.date if hasattr(b.date, "strftime") else datetime.fromisoformat(str(b.date))
        days[d.strftime("%Y-%m-%d")].append(float(b.close))
    return {k: v for k, v in days.items() if len(v) >= 20}


def _realized_vol(closes):
    """Annualised realized vol from 5-min log returns. 78 five-minute bars per RTH session, 252 days."""
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 10:
        return None
    return st.pstdev(rets) * math.sqrt(78 * 252)


def _barrier_pct(prem_pct, sigma_ann, dte_days):
    """Premium-% move -> underlying-% move, via the ATM map above. dte_days is the option's life."""
    T = max(dte_days, 0.5) / 252.0
    return (prem_pct / 100.0) * (SQRT_2PI_INV / ATM_DELTA) * sigma_ann * math.sqrt(T)


def _replay_day(closes, stop_b, t1_b, hold_bars, prem_per_move, stop_pct, t1_pct, cost_pct):
    """Walk one real session. Every bar is a candidate entry; the trade takes whichever barrier the
    ACTUAL path touches first inside the hold window, else marks out at the time stop.

    UNITS: PREMIUM-PERCENT, NOT R — and that choice is the whole correctness of this module. Scoring in
    R (P&L divided by the candidate stop) makes the metric itself a function of the setting being tested.
    Because 78-89% of these 20-minute trades never touch EITHER barrier, the score was dominated by the
    time-stop mark divided by the stop, so a smaller stop mechanically inflated it: NVDA measured +0.0302
    at stop 22, +0.0052 at 34, -0.0012 at 38 — perfectly monotone in 1/stop. All eight tickers then
    "chose" the same grid corner. That was arithmetic, not edge. Premium-% has a fixed denominator, so
    configurations are comparable, and the cost of the round trip is charged explicitly.
    """
    out = []
    for i in range(len(closes) - hold_bars):
        entry = closes[i]
        if entry <= 0:
            continue
        r = None
        for j in range(i + 1, i + 1 + hold_bars):
            mv = (closes[j] - entry) / entry
            if mv <= -stop_b:            # stop first
                r = -stop_pct
                break
            if mv >= t1_b:               # target first
                r = t1_pct
                break
        if r is None:                    # time stop: mark out at the prevailing premium
            mv = (closes[i + hold_bars] - entry) / entry
            r = max(-stop_pct, min(t1_pct, mv * prem_per_move))
        out.append(r - cost_pct)         # ask-in / bid-out / commission, charged once per trade
    return out


def _block_bootstrap(by_day, n_boot=3000, seed=7):
    """Resample DAYS with replacement (not trades). Returns (mean, se, lo, hi)."""
    import random
    rng = random.Random(seed)
    days = [v for v in by_day.values() if v]
    if len(days) < 3:
        return None
    flat = [x for d in days for x in d]
    mean = sum(flat) / len(flat)
    means = []
    for _ in range(n_boot):
        pick = [days[rng.randrange(len(days))] for _ in range(len(days))]
        vals = [x for d in pick for x in d]
        means.append(sum(vals) / len(vals))
    means.sort()
    return (mean, st.pstdev(means), means[int(0.025 * n_boot)], means[int(0.975 * n_boot)])


COMM_ROUNDTRIP_USD = 1.30        # $ per contract round trip, matching the buyer book
# Observed ATM spread as a % of premium, per ticker. These come from chip's own spread_max settings,
# which were themselves set from live quotes (index ETFs measured at 0.5-0.6%, single names capped
# 8-12%). Used only to charge a cost; a wrong value here shifts every row equally and cannot flip a
# ranking between two settings on the SAME ticker.
SPREAD_PCT = {"SPY": 0.6, "QQQ": 0.6, "IWM": 1.5, "AAPL": 3.0, "MSFT": 3.0, "GOOGL": 3.0,
              "AMZN": 3.0, "NVDA": 3.5, "META": 4.0, "TSLA": 4.5}
TYPICAL_PREM = {"SPY": 3.2, "QQQ": 4.8, "IWM": 1.6, "AAPL": 3.0, "MSFT": 4.0, "GOOGL": 3.0,
                "AMZN": 2.5, "NVDA": 4.0, "META": 9.0, "TSLA": 8.0}


def calibrate(ticker, ib, days_hist=90, dte_days=1):
    dur = f"{days_hist} D" if days_hist <= 365 else "1 Y"
    try:
        bars = MK.get_bars(ib, ticker, duration=dur, size="5 mins")
    except Exception as e:
        print(f"  {ticker}: bar fetch failed — {e}")
        return None
    sess = _sessions(bars)
    if len(sess) < 10:
        print(f"  {ticker}: only {len(sess)} sessions — too thin to calibrate")
        return None
    sigma = _realized_vol([c for v in sess.values() for c in v])
    if not sigma:
        print(f"  {ticker}: could not estimate vol")
        return None
    spread_pct = SPREAD_PCT.get(ticker, 3.0)
    spot_prem_usd = TYPICAL_PREM.get(ticker, 3.0)
    # premium-% per 1.0 of underlying fractional move — the inverse of _barrier_pct, used to mark the
    # time-stop exits in the same unit the barriers are quoted in.
    T = max(dte_days, 0.5) / 252.0
    prem_per_move = 100.0 / ((SQRT_2PI_INV / ATM_DELTA) * sigma * math.sqrt(T))
    # ROUND-TRIP COST in premium-%: cross the spread in and out, plus commission. Charged on every
    # trade, because a grid that ignores cost always prefers the setting that trades most.
    cost_pct = spread_pct + (COMM_ROUNDTRIP_USD / (spot_prem_usd * 100.0)) * 100.0
    results = []
    for stop_pct in STOP_GRID:
        for ratio in T1_RATIOS:
            t1_pct = round(stop_pct * ratio)
            sb = _barrier_pct(stop_pct, sigma, dte_days)
            tb = _barrier_pct(t1_pct, sigma, dte_days)
            if sb <= 0 or tb <= 0:
                continue
            by_day = {d: _replay_day(c, sb, tb, BARS_PER_HOLD, prem_per_move,
                                     stop_pct, t1_pct, cost_pct) for d, c in sess.items()}
            bs = _block_bootstrap(by_day)
            if not bs:
                continue
            mean, se, lo, hi = bs
            results.append({"stop_pct": stop_pct, "t1_pct": t1_pct, "ratio": ratio,
                            "meanR": round(mean, 4), "se": round(se, 4),
                            "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                            "n_trades": sum(len(v) for v in by_day.values()), "n_days": len(by_day)})
    if not results:
        return None
    results.sort(key=lambda r: -r["meanR"])
    best = results[0]
    return {"ticker": ticker, "sessions": len(sess), "realized_vol": round(sigma, 4),
            "best": best, "grid": results,
            # A result that cannot be distinguished from zero is noise. Say so on the row — but test BOTH
            # directions. `ci_lo > 0` alone labelled a confidently NEGATIVE result (IWM [-2.50, -1.20],
            # nowhere near zero) as "includes 0", which reads as "no signal" when the finding is in fact
            # a firm one: this configuration loses money. A one-sided significance test on a two-sided
            # question hides exactly the answer you most need.
            "significant": bool(best["ci_lo"] > 0 or best["ci_hi"] < 0),
            "sign": "positive" if best["ci_lo"] > 0 else ("NEGATIVE" if best["ci_hi"] < 0 else "unclear")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--dte", type=int, default=1)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    tks = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    ib = MK.connect(client_id=CLIENT_ID)
    out, all_res = {}, []
    print(f"calibrating {len(tks)} tickers on {a.days}D of 5-min bars, {a.dte}DTE map, "
          f"{BARS_PER_HOLD*5}-min hold\n")
    for tk in tks:
        r = calibrate(tk, ib, a.days, a.dte)
        if not r:
            continue
        all_res.append(r)
        b = r["best"]
        flag = {"positive": "", "NEGATIVE": "   <-- CONFIDENTLY NEGATIVE (best of grid still loses)",
                "unclear": "   (CI spans 0 — not distinguishable from noise)"}[r["sign"]]
        print(f"  {tk:6s} {r['sessions']:3d} sess  vol {r['realized_vol']:.2f}  ->  "
              f"stop {b['stop_pct']} t1 {b['t1_pct']} ({b['ratio']:.1f}x)  "
              f"best {b['meanR']:+.2f}% [{b['ci_lo']:+.2f}, {b['ci_hi']:+.2f}] per trade  "
              f"n={b['n_trades']}/{b['n_days']}d{flag}")
        if r["sign"] == "NEGATIVE":
            # REFUSE TO WRITE a setting whose own measurement says it loses. The grid's "best" is only
            # the least-bad; persisting it would dress a losing configuration as a calibrated one.
            print(f"         ^ not written: every setting in the grid is negative for {tk}")
            continue
        out[tk] = {"stop_pct": b["stop_pct"], "t1_pct": b["t1_pct"],
                   "t2_pct": round(b["t1_pct"] * 2), "t3_pct": round(b["t1_pct"] * 3.1),
                   "_meanR": b["meanR"], "_ci": [b["ci_lo"], b["ci_hi"]],
                   "_sessions": r["sessions"], "_significant": r["significant"]}
    if a.write and out:
        payload = {"_generated": datetime.now().isoformat(), "_method": "underlying-path replay, "
                   "ATM premium->underlying barrier map, day-block bootstrap. RANKING of settings under "
                   "a common assumption — NOT a P&L forecast. No theta, no IV path, no spread widening. "
                   "Superseded by real stored option quotes once enough sessions exist.",
                   "_days_history": a.days, "_dte": a.dte, "tickers": out}
        OUT.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {OUT} ({len(out)} tickers)")
    elif out:
        print("\n(dry run — pass --write to persist to chip_mechanics.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
