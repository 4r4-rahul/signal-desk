#!/usr/bin/env python3
"""
IBKR market-data + technical indicators for the confidence engine.

Pulls intraday bars from TWS/IB Gateway and computes VWAP, RSI(14),
Bollinger(20,2), EMA9/21 → a TECHNICAL ALIGNMENT read for a call/put signal.

These are CONFIRMATION heuristics (does the signal agree with momentum?), not a
validated predictor — used to nudge confidence and flag direction, transparently.

Test:  .venv/bin/python market.py SPY
Needs: TWS/IB Gateway running with API enabled (Configure → API → Enable Socket Clients).
"""
import os, sys, asyncio
asyncio.set_event_loop(asyncio.new_event_loop())      # Python 3.14 needs an explicit loop
import numpy as np
from pathlib import Path


def env(k, d=None):
    for ln in (Path(".env").read_text().splitlines() if Path(".env").exists() else []):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln and ln.split("=", 1)[0].strip() == k:
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(k, d)


def connect(client_id=77):
    from ib_async import IB
    ib = IB()
    ib.connect(env("IBKR_HOST", "127.0.0.1"), int(env("IBKR_PORT", "7497")),
               clientId=client_id, timeout=8, readonly=True)
    try:
        ib.reqMarketDataType(int(env("IBKR_MARKET_DATA_TYPE", "3")))
    except Exception:
        pass
    return ib


def equity(ib=None):
    """Live account value (NetLiquidation) from IBKR — the basis for compounding."""
    own = ib is None
    try:
        if own:
            ib = connect()
        for v in ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                return float(v.value)
        return None
    except Exception:
        return None
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass


def _pick_standard(contracts, tk):
    """From candidate option contracts (same strike/right/expiry), pick the STANDARD listing and
    skip corporate-action adjusted classes. The standard equity option class equals the root symbol
    (NVDA), trades 100 shares/contract; adjusted classes look like 2NVDA / NVDA1 with odd multipliers."""
    if not contracts:
        return None
    def rank(c):
        std_class = (c.tradingClass or "").upper() == tk        # standard class == root symbol
        std_mult = str(c.multiplier or "100") in ("100", "")    # normal 100-share deliverable
        return (not std_class, not std_mult, len(c.tradingClass or ""))  # lower is better
    return sorted(contracts, key=rank)[0]


def option_quote(ticker, strike, right, expiry=None, ib=None):
    """Live option price + Greeks from IBKR for a specific contract."""
    own = ib is None
    from ib_async import Option
    try:
        if own:
            ib = connect()
        r = right.upper()[0]
        tk = ticker.upper()
        exch = "CBOE" if tk in ("SPX", "SPXW", "VIX", "NDX", "RUT") else "SMART"
        tclass = "SPXW" if tk == "SPX" else ""
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y%m%d")
        # Pull ALL contract details for this strike/right, then pick the STANDARD class ourselves.
        # A stock that had a corporate action (split, special dividend) carries an adjusted option
        # class like "2NVDA"/"NVDA1" alongside the standard "NVDA" one. Qualifying with a blank
        # tradingClass makes IBKR return both and either error (ambiguous) or pick the odd lot.
        base = Option(tk, expiry or "", float(strike), r, exchange=exch, tradingClass=tclass)
        cds = [cd.contract for cd in ib.reqContractDetails(base)]
        if not cds:
            return {"error": "contract not found at IBKR"}
        if not expiry:                              # resolve nearest NON-expired expiry
            exps = sorted(e for e in {c.lastTradeDateOrContractMonth for c in cds} if e >= today)
            if not exps:
                return {"error": "no valid expiries found"}
            expiry = exps[0]
        cands = [c for c in cds if c.lastTradeDateOrContractMonth == expiry]
        opt = _pick_standard(cands, tk) or (cands[0] if cands else None)
        if opt is None:
            return {"error": "contract not found at IBKR"}
        t = ib.reqMktData(opt, "", False, False)
        ib.sleep(2.5)
        bid, ask, last = t.bid, t.ask, (t.last if t.last == t.last else t.close)
        mid = (bid + ask) / 2 if (bid and ask and bid > 0 and ask > 0) else last
        q = {"expiry": expiry, "bid": bid, "ask": ask, "last": last, "mid": round(mid, 2) if mid else None,
             "spread_pct": round((ask - bid) / mid * 100, 1) if (mid and ask and bid and bid > 0) else None}
        if t.modelGreeks:
            g = t.modelGreeks
            q.update(iv=round(g.impliedVol, 3) if g.impliedVol else None,
                     delta=round(g.delta, 3) if g.delta else None,
                     gamma=round(g.gamma, 4) if g.gamma else None,
                     theta=round(g.theta, 4) if g.theta else None,
                     vega=round(g.vega, 4) if g.vega else None)
        ib.cancelMktData(opt)
        return q
    except Exception as e:
        return {"error": str(e)}
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass


def _contract(ticker):
    from ib_async import Stock, Index
    t = ticker.upper()
    if t in ("SPX", "VIX", "NDX", "RUT"):
        return Index(t, "CBOE", "USD")
    if t in ("SPY", "QQQ", "IWM", "DIA"):
        return Stock(t, "ARCA", "USD")
    return Stock(t, "SMART", "USD")


def spot_quote(ticker, ib=None):
    """FAST last-price snapshot of the UNDERLYING (reqMktData) for real-time stop-watching + card
    display. Far lighter than get_bars/indicators — no history request, just the current print.
    Returns a rounded float or None."""
    own = ib is None
    try:
        if ib is None:
            ib = connect()
        c = _contract(ticker)
        ib.qualifyContracts(c)
        t = ib.reqMktData(c, "", False, False)
        ib.sleep(1.0)
        px = t.last if (t.last == t.last and t.last and t.last > 0) else (t.close if t.close == t.close else None)
        try: ib.cancelMktData(c)
        except Exception: pass
        return round(px, 2) if px else None
    except Exception:
        return None
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass


def get_bars(ib, ticker, duration="1 D", size="5 mins"):
    c = _contract(ticker)
    ib.qualifyContracts(c)
    bars = ib.reqHistoricalData(c, endDateTime="", durationStr=duration,
                                barSizeSetting=size, whatToShow="TRADES",
                                useRTH=True, formatDate=1)
    return bars


def _ema(x, n):
    a = 2 / (n + 1); e = x[0]
    for v in x[1:]:
        e = a * v + (1 - a) * e
    return e


def _rsi(closes, n=14):
    d = np.diff(closes)
    if len(d) < n + 1:
        return None
    up = np.clip(d, 0, None); dn = -np.clip(d, None, 0)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    rs = au / ad if ad > 1e-9 else 999
    return round(100 - 100 / (1 + rs), 1)


def indicators(bars):
    if not bars or len(bars) < 20:
        return None
    c = np.array([b.close for b in bars], float)
    h = np.array([b.high for b in bars], float)
    l = np.array([b.low for b in bars], float)
    v = np.array([b.volume for b in bars], float)
    tp = (h + l + c) / 3
    vwap = float((tp * v).sum() / v.sum()) if v.sum() > 0 else float(c[-1])
    sma20, sd20 = float(c[-20:].mean()), float(c[-20:].std())
    return {"price": round(float(c[-1]), 2), "vwap": round(vwap, 2),
            "rsi": _rsi(c), "ema9": round(_ema(c[-30:], 9), 2), "ema21": round(_ema(c[-40:], 21), 2),
            "bb_up": round(sma20 + 2 * sd20, 2), "bb_mid": round(sma20, 2), "bb_low": round(sma20 - 2 * sd20, 2)}


def technical(sig_type, ind):
    """Return (score_adjustment[-15..+15], label, notes[]) for a call/put vs the tape."""
    if not ind or ind["rsi"] is None:
        return 0, "no data", []
    call = sig_type == "C"
    p, vwap, rsi = ind["price"], ind["vwap"], ind["rsi"]
    up_trend = ind["ema9"] > ind["ema21"]
    sc, notes = 0, []
    # trend vs VWAP
    if (p > vwap) == call: sc += 6; notes.append(f"price {'>' if p>vwap else '<'} VWAP ✓")
    else: sc -= 6; notes.append(f"price {'>' if p>vwap else '<'} VWAP ✗")
    # EMA trend
    if up_trend == call: sc += 4; notes.append(f"EMA9{'>' if up_trend else '<'}EMA21 ✓")
    else: sc -= 4; notes.append(f"EMA9{'>' if up_trend else '<'}EMA21 ✗")
    # RSI: penalize chasing an exhausted move
    if call and rsi > 78: sc -= 5; notes.append(f"RSI {rsi} overbought ✗")
    elif not call and rsi < 22: sc -= 5; notes.append(f"RSI {rsi} oversold ✗")
    elif (call and 45 <= rsi <= 72) or (not call and 28 <= rsi <= 55): sc += 5; notes.append(f"RSI {rsi} healthy ✓")
    else: notes.append(f"RSI {rsi} neutral")
    # Bollinger extreme
    if call and p >= ind["bb_up"]: sc -= 3; notes.append("at upper Bollinger ✗")
    if not call and p <= ind["bb_low"]: sc -= 3; notes.append("at lower Bollinger ✗")
    label = "ALIGNED ✓" if sc >= 6 else "AGAINST ✗" if sc <= -6 else "MIXED ~"
    return max(-15, min(15, sc)), label, notes


def tape_strength(sig_type, ind):
    """Grade HOW STRONGLY the tape backs this direction — MAGNITUDE, not just yes/no. The binary
    ALIGNED ✓ can't tell 'barely above VWAP, RSI 51' (weak) from 'price well past VWAP, EMAs fanned,
    RSI 63, room to run' (strong). This can. Returns
    {score:0-100|None, grade:'strong'|'moderate'|'weak'|'against'|'no-data', icon, bars, components:[...]}.
    Composes with the ALIGNED/MIXED/AGAINST direction, e.g. 'ALIGNED · ⚪ Weak'."""
    if not ind or ind.get("rsi") is None or not ind.get("vwap") or not ind.get("price"):
        return {"score": None, "grade": "no-data", "icon": "·", "bars": "", "components": []}
    call = (sig_type or "C") == "C"
    p, vwap, rsi = ind["price"], ind["vwap"], ind["rsi"]
    e9, e21, bbu, bbl = ind.get("ema9"), ind.get("ema21"), ind.get("bb_up"), ind.get("bb_low")
    cl = lambda x: max(-1.0, min(1.0, x))
    comps = []
    # 1) VWAP distance IN THE TRADE'S FAVOR, % of price (full strength ~0.6% beyond)
    vfav = ((p - vwap) / vwap * 100) * (1 if call else -1)
    v = cl(vfav / 0.6)
    comps.append({"k": "VWAP", "s": round(v, 2), "t": f"{vfav:+.2f}% {'past VWAP' if vfav >= 0 else 'wrong side of VWAP'}"})
    # 2) EMA9-vs-EMA21 separation, % of price, in favor (full ~0.5%)
    efav = (((e9 - e21) / p * 100) * (1 if call else -1)) if (e9 and e21 and p) else 0.0
    e = cl(efav / 0.5)
    comps.append({"k": "EMA", "s": round(e, 2), "t": f"EMA9 {'>' if (e9 or 0) > (e21 or 0) else '<'} EMA21 ({efav:+.2f}%)"})
    # 3) RSI momentum toward the trade — into the healthy band, penalize exhaustion
    r = ((rsi - 50) / 16 if rsi <= 72 else (72 - rsi) / 8) if call else ((50 - rsi) / 16 if rsi >= 28 else (rsi - 28) / 8)
    r = cl(r)
    comps.append({"k": "RSI", "s": round(r, 2), "t": f"RSI {rsi:.0f}"})
    # 4) Bollinger room to run toward the trade (mid-channel best; pinned at the far band = extended)
    if bbu and bbl and bbu > bbl:
        pos = (p - bbl) / (bbu - bbl)                          # 0 at lower band .. 1 at upper
        room = 1 - max(0.0, (pos - 0.5) * 2) if call else 1 - max(0.0, (0.5 - pos) * 2)
        rm = cl(room * 2 - 1)
        comps.append({"k": "room", "s": round(rm, 2), "t": ("room to run" if rm > -0.2 else ("pinned at upper BB" if call else "pinned at lower BB"))})
    else:
        rm = 0.0
    raw = 0.35 * v + 0.25 * e + 0.25 * r + 0.15 * rm           # -1..+1
    score = round((raw + 1) / 2 * 100)                          # 0..100
    if raw < -0.05:    grade, icon = "against", "🔴"
    elif score >= 74:  grade, icon = "strong", "🟢"
    elif score >= 62:  grade, icon = "moderate", "🟡"
    else:              grade, icon = "weak", "⚪"
    filled = max(0, min(5, round(score / 20)))
    return {"score": score, "grade": grade, "icon": icon,
            "bars": "▮" * filled + "▯" * (5 - filled), "components": comps}


def option_score(q):
    """Fold live option QUALITY (bid/ask spread + delta + IV) into confidence.
    Returns (adj[-12..+10], label, notes[]). Spread/delta are live from IBKR; IV shown, not ranked."""
    if not q or q.get("error"):
        return 0, None, []
    sc, notes = 0, []
    sp = q.get("spread_pct")
    if sp is not None:
        if sp > 15: sc -= 8; notes.append(f"wide spread {sp}% ✗")
        elif sp > 8: sc -= 4; notes.append(f"spread {sp}% ~")
        elif sp <= 5: sc += 3; notes.append(f"tight spread {sp}% ✓")
    d = q.get("delta")
    if d is not None:
        ad = abs(d)
        if ad < 0.15: sc -= 6; notes.append(f"Δ{d} far-OTM lotto ✗")
        elif 0.35 <= ad <= 0.65: sc += 4; notes.append(f"Δ{d} balanced ✓")
        elif ad > 0.85: sc -= 2; notes.append(f"Δ{d} deep ITM/pricey")
        else: notes.append(f"Δ{d}")
    if q.get("iv") is not None:
        notes.append(f"IV {round(q['iv']*100)}%")
    sc = max(-12, min(10, sc))
    label = "option ok ✓" if sc >= 3 else "option poor ✗" if sc <= -6 else "option fair ~"
    return sc, label, notes


def realized_vol(bars):
    """Annualized realized volatility from intraday bars — feeds the dynamic-stop vol band.
    Using max(IV, realized_vol) means a FAST tape widens the stop before IV re-marks."""
    if not bars or len(bars) < 20:
        return None
    c = np.array([b.close for b in bars], float)
    r = np.diff(np.log(c))
    return float(r.std() * np.sqrt(78 * 252))             # ~78 five-min bars/day, annualized


def context(ticker, sig_type, ib=None, strike=None, expiry=None):
    """One-shot: connect (if needed), pull bars, return indicators + technical + option read + realized vol.
    `expiry` (yyyymmdd) prices the SIGNAL'S contract — without it the nearest/0DTE is used (wrong for 7/9 etc.)."""
    own = ib is None
    try:
        if own: ib = connect()
        eq = equity(ib)                                   # live balance (for compounding)
        bars = get_bars(ib, ticker)
        ind = indicators(bars)
        rv = realized_vol(bars)
        adj, label, notes = technical(sig_type, ind)
        strength = tape_strength(sig_type, ind)           # graded conviction of the aligned tape
        q, oadj, olabel = None, 0, None
        if strike:                                        # live option Greeks -> quality adjustment
            q = option_quote(ticker, strike, sig_type, expiry=expiry, ib=ib)
            oadj, olabel, onotes = option_score(q)
            adj += oadj; notes = notes + onotes
        return {"ok": True, "ind": ind, "rv": rv, "adj": adj, "label": label, "strength": strength,
                "notes": notes, "equity": eq, "opt": q, "opt_adj": oadj, "opt_label": olabel}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass


if __name__ == "__main__":
    tk = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    typ = (sys.argv[2] if len(sys.argv) > 2 else "C").upper()
    print(f"Connecting to IBKR at {env('IBKR_HOST','127.0.0.1')}:{env('IBKR_PORT','7497')} …")
    r = context(tk, typ)
    if not r["ok"]:
        print("❌", r["error"]); sys.exit(1)
    print(f"\n{tk}  indicators:")
    for k, val in r["ind"].items():
        print(f"   {k:<6} {val}")
    print(f"\nTechnical read for a {tk} {'CALL' if typ=='C' else 'PUT'}: {r['label']}  (conf adj {r['adj']:+d})")
    for n in r["notes"]:
        print("   ·", n)
