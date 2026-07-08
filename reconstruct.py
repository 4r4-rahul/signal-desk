#!/usr/bin/env python3
"""
Reconstruct each trade's option price path from SPY daily OHLC, then simulate
exit rules to find the one that maximizes expectancy.

Method (free-data, daily granularity):
  1. S0 = SPY price at entry (proxy from entry-day OHLC by time of day).
  2. Back out implied vol so Black-Scholes reprices the KNOWN entry premium.
  3. Walk each trading day to expiry; reprice the option at that day's favorable
     and adverse extreme -> gives Max Favorable / Adverse Excursion (MFE/MAE).
  4. Simulate exit rules. Where a target and stop are both reachable in the same
     day, daily data can't say which came first -> we report an OPTIMISTIC
     (target-first) and PESSIMISTIC (stop-first) bound.

Heavily approximate (no intraday path, IV assumed flat) but UNBIASED: every
trade is evaluated, winners and losers alike.
"""
import sqlite3, json, math, sys
from datetime import date, timedelta
from pathlib import Path

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "jmt"
DB_PATH = str(Path(__file__).parent / "channels" / CHANNEL / f"{CHANNEL}.db")

R, Q = 0.045, 0.013          # risk-free, SPY dividend yield
SPY = {d: v for d, v in json.load(open("data/spy_daily.json")).items()}
DAYS = sorted(SPY)


def _N(x):                    # standard normal CDF via erf (stdlib only)
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, vol, call):
    if T <= 0 or vol <= 0:
        intr = max(0.0, (S - K) if call else (K - S))
        return intr
    d1 = (math.log(S / K) + (R - Q + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if call:
        return S * math.exp(-Q * T) * _N(d1) - K * math.exp(-R * T) * _N(d2)
    return K * math.exp(-R * T) * _N(-d2) - S * math.exp(-Q * T) * _N(-d1)


def implied_vol(price, S, K, T, call):
    intr = max(0.0, (S - K) if call else (K - S))
    if price <= intr + 1e-6:
        return None                      # premium at/below intrinsic -> can't solve
    lo, hi = 1e-3, 6.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def trading_days(start, end):
    s, e = start.isoformat(), end.isoformat()
    return [d for d in DAYS if s <= d <= e]


def spot_at_entry(day, hour):
    o = SPY.get(day)
    if not o:
        return None
    if hour < 11:     return o['o']
    if hour >= 15:    return o['c']
    return round((o['h'] + o['l']) / 2, 2)


def remaining_T(d, expiry, entry_hour=None, entry_day=False):
    days = (expiry - d).days
    frac = (16 - entry_hour) / 6.5 if (entry_day and entry_hour is not None) else 1.0
    frac = min(max(frac, 0.05), 1.0)
    return max((days - 1 + frac) / 365.0, 0.5 / 365.0)


def build_path(t):
    """Return list of (day, fav_val, adv_val) plus expiry value, or None if unmodelable."""
    entry_day = date.fromisoformat(t['entry_ts'][:10])
    expiry = date.fromisoformat(t['expiry'])
    call = t['type'] == 'C'
    S0 = spot_at_entry(entry_day.isoformat(), t['hour'])
    if S0 is None:
        return None
    T0 = remaining_T(entry_day, expiry, t['hour'], entry_day=True)
    vol = implied_vol(t['premium'], S0, t['strike'], T0, call)
    if vol is None or vol > 5:
        return None

    path = []
    for d in trading_days(entry_day, expiry):
        ohlc = SPY[d]
        is_entry = (d == entry_day.isoformat())
        T = remaining_T(date.fromisoformat(d), expiry, t['hour'], entry_day=is_entry)
        # favorable underlying for a call = high; for a put = low
        S_fav = ohlc['h'] if call else ohlc['l']
        S_adv = ohlc['l'] if call else ohlc['h']
        fav = bs(S_fav, t['strike'], T, vol, call)
        adv = bs(S_adv, t['strike'], T, vol, call)
        path.append((d, fav, adv))
    # expiry settle (intrinsic at expiry-day close)
    last = SPY[trading_days(entry_day, expiry)[-1]]
    exp_val = max(0.0, (last['c'] - t['strike']) if call else (t['strike'] - last['c']))
    return dict(S0=S0, vol=vol, path=path, exp_val=exp_val)


def simulate(t, pth, tp_pct, stop_pct):
    """Walk days in order. Return (opt_ret, pess_ret) as fractions of premium."""
    C0 = t['premium']
    target = C0 * (1 + tp_pct)
    stop = C0 * (1 + stop_pct)            # stop_pct negative
    for (d, fav, adv) in pth['path']:
        hit_t = fav >= target
        hit_s = adv <= stop
        if hit_t and hit_s:
            return (tp_pct, stop_pct)     # ambiguous within the day
        if hit_t:
            return (tp_pct, tp_pct)
        if hit_s:
            return (stop_pct, stop_pct)
    r = pth['exp_val'] / C0 - 1
    return (r, r)


def stats(returns):
    n = len(returns)
    if not n:
        return None
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gp = sum(wins); gl = -sum(losses)
    return dict(n=n, exp=sum(returns) / n, win=len(wins) / n,
                avg_w=(gp / len(wins) if wins else 0),
                avg_l=(gl / len(losses) if losses else 0),
                pf=(gp / gl if gl > 0 else float('inf')))


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades")]

    paths, skipped = [], 0
    for t in trades:
        p = build_path(t)
        if p is None:
            skipped += 1
            continue
        paths.append((t, p))
    print(f"Modeled {len(paths)} of {len(trades)} trades ({skipped} unmodelable).")

    # validation: reconstructed MFE distribution vs chat's posted peaks (~median +30%)
    mfes = sorted(max((fav / t['premium'] - 1) for (_, fav, _) in p['path']) for t, p in paths)
    m = mfes[len(mfes)//2]
    print(f"Reconstructed MFE (max gain reachable) median: +{m*100:.0f}%  "
          f"[25%={mfes[len(mfes)//4]*100:+.0f}  75%={mfes[3*len(mfes)//4]*100:+.0f}]")
    print("(chat brags clustered ~+30% median -> sanity check)\n")

    STOP = -0.50    # default premium stop when channel SL absent
    print(f"{'EXIT RULE':<22}{'n':>5}{'EXPECTANCY':>22}{'WIN%':>7}{'AVG W':>8}{'AVG L':>8}{'PF':>7}")
    print("-" * 79)

    # hold to expiry
    rets = [pth['exp_val'] / t['premium'] - 1 for t, pth in paths]
    s = stats(rets)
    print(f"{'Hold to expiry':<22}{s['n']:>5}{s['exp']*100:>20.1f}%{s['win']*100:>7.0f}"
          f"{s['avg_w']*100:>7.0f}%{s['avg_l']*100:>7.0f}%{s['pf']:>7.2f}")

    # fixed take-profit sweep (with -50% premium stop)
    for tp in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00):
        opt = [simulate(t, pth, tp, STOP)[0] for t, pth in paths]
        pess = [simulate(t, pth, tp, STOP)[1] for t, pth in paths]
        so, sp = stats(opt), stats(pess)
        rng = f"{sp['exp']*100:>+6.1f}..{so['exp']*100:<+6.1f}%"
        print(f"{f'TP +{int(tp*100)}% / stop -50%':<22}{so['n']:>5}{rng:>22}{so['win']*100:>7.0f}"
              f"{so['avg_w']*100:>7.0f}%{so['avg_l']*100:>7.0f}%{so['pf']:>7.2f}")

    # scale-out (the trader's own style): bank half at +30%, runner to +75% or -50% stop
    def scaleout(t, pth, tp1=0.30, tp2=0.75, stop=-0.50):
        C0 = t['premium']; tgt1, tgt2, stp = C0*(1+tp1), C0*(1+tp2), C0*(1+stop)
        banked = None; opt = pess = None
        for i, (d, fav, adv) in enumerate(pth['path']):
            if banked is None:
                if fav >= tgt1 and adv <= stp:          # ambiguous first leg
                    o = 0.5*tp1 + 0.5*tp2; p = 0.5*stop + 0.5*stop
                    # optimistic: bank half then runner also tops; pessimistic: stopped whole
                    return (0.5*tp1 + 0.5*tp2, stop)
                if fav >= tgt1:
                    banked = 0.5*tp1                      # half booked, runner continues this day
                    if fav >= tgt2: return (banked+0.5*tp2, banked+0.5*tp2)
                    if adv <= stp:  return (banked+0.5*stop, banked+0.5*stop)
                    continue
                if adv <= stp:
                    return (stop, stop)
            else:
                if fav >= tgt2 and adv <= stp: return (banked+0.5*tp2, banked+0.5*stop)
                if fav >= tgt2: return (banked+0.5*tp2, banked+0.5*tp2)
                if adv <= stp:  return (banked+0.5*stop, banked+0.5*stop)
        r = pth['exp_val']/t['premium'] - 1
        return ((banked + 0.5*r) if banked is not None else r,
                (banked + 0.5*r) if banked is not None else r)

    so = stats([scaleout(t, pth)[0] for t, pth in paths])
    sp = stats([scaleout(t, pth)[1] for t, pth in paths])
    rng = f"{sp['exp']*100:>+6.1f}..{so['exp']*100:<+6.1f}%"
    print(f"{'Scale 1/2@+30,run+75':<22}{so['n']:>5}{rng:>22}"
          f"{so['win']*100:>7.0f}{so['avg_w']*100:>7.0f}%{so['avg_l']*100:>7.0f}%{so['pf']:>7.2f}")

    print("\n(EXPECTANCY shown as pessimistic..optimistic range per trade, as % of premium risked.)")
    print("Win%/AvgW/AvgL/PF shown for the optimistic case.\n")

    # breakdowns under a fixed +40% TP rule (relative comparison is robust to the band)
    def grp(pred):
        sub = [(t, pth) for t, pth in paths if pred(t)]
        if not sub: return None
        o = stats([simulate(t, pth, 0.40, STOP)[0] for t, pth in sub])
        p = stats([simulate(t, pth, 0.40, STOP)[1] for t, pth in sub])
        return o, p, len(sub)
    print("BREAKDOWN @ TP+40%/stop-50% (pessimistic..optimistic expectancy):")
    for label, pred in [("Calls", lambda t: t['type']=='C'),
                        ("Puts", lambda t: t['type']=='P'),
                        ("Morning (<12 ET)", lambda t: t['hour']<12),
                        ("Power hour (>=14)", lambda t: t['hour']>=14),
                        ("0-1 DTE", lambda t: t['dte']<=1),
                        ("2+ DTE", lambda t: t['dte']>=2)]:
        g = grp(pred)
        if g:
            o, p, n = g
            print(f"  {label:<20} n={n:>4}  {p['exp']*100:>+6.1f}..{o['exp']*100:<+6.1f}%   win {o['win']*100:.0f}%")


if __name__ == "__main__":
    main()
