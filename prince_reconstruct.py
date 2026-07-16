#!/usr/bin/env python3
"""Multi-ticker Black-Scholes reconstruction of PRINCE's edge (same engine as JMT's reconstruct.py,
generalized to per-ticker price paths). Fetches daily OHLC for each ticker Prince trades, reprices
every option along its underlying's path, simulates his scale-out exit. Reports win% + expectancy.
Also models his CHEAP subset (premium < $1) to represent the Small Account Challenge (= same trader)."""
import json, math, os, urllib.request, statistics as st
from datetime import date, datetime, timezone

R, Q = 0.045, 0.0


def fetch_daily(tk):
    p = f"data/{tk}_daily.json"
    if os.path.exists(p):
        return json.load(open(p))
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}?range=5y&interval=1d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        r = d["chart"]["result"][0]; ts = r["timestamp"]; q = r["indicators"]["quote"][0]
        out = {}
        for i, t in enumerate(ts):
            if q["close"][i] is None:
                continue
            dd = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
            out[dd] = {"o": round(q["open"][i], 2), "h": round(q["high"][i], 2),
                       "l": round(q["low"][i], 2), "c": round(q["close"][i], 2)}
        json.dump(out, open(p, "w"))
        return out
    except Exception as e:
        print(f"  fetch {tk} FAILED: {e}")
        return {}


def _N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def bs(S, K, T, vol, call):
    if T <= 0 or vol <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (R - Q + 0.5 * vol * vol) * T) / (vol * math.sqrt(T)); d2 = d1 - vol * math.sqrt(T)
    return (S * _N(d1) - K * math.exp(-R * T) * _N(d2)) if call else (K * math.exp(-R * T) * _N(-d2) - S * _N(-d1))
def implied_vol(price, S, K, T, call):
    if price <= max(0.0, (S - K) if call else (K - S)) + 1e-6:
        return None
    lo, hi = 1e-3, 6.0
    for _ in range(60):
        mid = (lo + hi) / 2
        hi, lo = (mid, lo) if bs(S, K, T, mid, call) > price else (hi, mid)
    return (lo + hi) / 2


def spot_at_entry(o, hour):
    if hour < 11: return o["o"]
    if hour >= 15: return o["c"]
    return round((o["h"] + o["l"]) / 2, 2)
def remT(d, expiry, hour, entry_day):
    frac = min(max((16 - hour) / 6.5 if (entry_day and hour is not None) else 1.0, 0.05), 1.0)
    return max(((expiry - d).days - 1 + frac) / 365.0, 0.5 / 365.0)


def build_path(t, PX, DAYS):
    ed = date.fromisoformat(t["entry_ts"][:10]); exp = date.fromisoformat(t["expiry"]); call = t["type"] == "C"
    o = PX.get(ed.isoformat())
    if not o: return None
    S0 = spot_at_entry(o, t["hour"]); T0 = remT(ed, exp, t["hour"], True)
    vol = implied_vol(t["premium"], S0, t["strike"], T0, call)
    if vol is None or vol > 5: return None
    days = [d for d in DAYS if ed.isoformat() <= d <= exp.isoformat()]
    if not days: return None
    path = []
    for d in days:
        oh = PX[d]; ise = d == ed.isoformat(); T = remT(date.fromisoformat(d), exp, t["hour"], ise)
        Sf, Sa = (oh["h"], oh["l"]) if call else (oh["l"], oh["h"])
        path.append((d, bs(Sf, t["strike"], T, vol, call), bs(Sa, t["strike"], T, vol, call)))
    lc = PX[days[-1]]["c"]; exp_val = max(0.0, (lc - t["strike"]) if call else (t["strike"] - lc))
    return {"path": path, "exp_val": exp_val}


def scaleout(t, pth, tp1=0.30, tp2=0.75, stop=-0.50):
    C0 = t["premium"]; g1, g2, sp = C0 * (1 + tp1), C0 * (1 + tp2), C0 * (1 + stop); banked = None
    for (d, fav, adv) in pth["path"]:
        if banked is None:
            if fav >= g1 and adv <= sp: return (0.5 * tp1 + 0.5 * tp2, stop)
            if fav >= g1:
                banked = 0.5 * tp1
                if fav >= g2: return (banked + 0.5 * tp2,) * 2
                if adv <= sp: return (banked + 0.5 * stop,) * 2
                continue
            if adv <= sp: return (stop, stop)
        else:
            if fav >= g2 and adv <= sp: return (banked + 0.5 * tp2, banked + 0.5 * stop)
            if fav >= g2: return (banked + 0.5 * tp2,) * 2
            if adv <= sp: return (banked + 0.5 * stop,) * 2
    r = pth["exp_val"] / t["premium"] - 1
    return ((banked + 0.5 * r) if banked is not None else r,) * 2


def report(name, rets):
    if not rets:
        print(f"  {name}: no modelable trades"); return
    n = len(rets); win = sum(1 for r in rets if r > 0) / n
    exp = st.mean(rets)
    print(f"  {name:26} n={n:3}  win {win*100:3.0f}%  expectancy {exp*100:+5.1f}% of premium  (=> {exp/0.5:+.2f}R at −50% stop)")
    return exp


if __name__ == "__main__":
    trades = json.load(open("data/prince_trades.json"))
    tickers = sorted({t["ticker"] for t in trades})
    print(f"Fetching daily data for {len(tickers)} tickers: {', '.join(tickers)}")
    DATA = {}
    for tk in tickers:
        px = fetch_daily(tk)
        if px: DATA[tk] = (px, sorted(px))
    print(f"  got data for {len(DATA)}/{len(tickers)} tickers\n")

    all_r, cheap_r, mid_r = [], [], []
    modeled = 0
    for t in trades:
        if t["ticker"] not in DATA: continue
        PX, DAYS = DATA[t["ticker"]]
        p = build_path(t, PX, DAYS)
        if p is None: continue
        modeled += 1
        o, pe = scaleout(t, p); r = (o + pe) / 2  # MID
        all_r.append(r)
        (cheap_r if t["premium"] < 1.0 else mid_r).append(r)   # cheap = challenge-style

    print(f"Modeled {modeled}/{len(trades)} trades (rest: missing price data / unsolvable IV)\n")
    print("=== PRINCE reconstruction (scale-out ½@+30 / run+75 / −50% stop, MID case) ===")
    report("PRINCE (all)", all_r)
    report("PRINCE cheap <$1 (=challenge)", cheap_r)
    report("PRINCE $1+ (main)", mid_r)
