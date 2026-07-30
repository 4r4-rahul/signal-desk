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
    if os.environ.get("DESK_ENV") == "test" and not os.environ.get("DESK_ALLOW_LIVE_BROKER"):
        raise RuntimeError("market.connect() refused: DESK_ENV=test — live IBKR socket blocked (set DESK_ALLOW_LIVE_BROKER=1 to override)")
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


import time as _time
# PERSISTENT streaming pool. reqMktData(snapshot=False) returns a Ticker IB mutates in place; we keep it
# alive and READ it (no per-call sleep/cancel) — turning request/response polling back into a push feed.
# Keyed per (connection, conId) so each loop-thread owns its own lines. FALLS BACK to a one-shot snapshot
# on a fresh/own connection, so nothing ever goes dark. reap_subs() governs the ~100-line IBKR budget.
_SUBS = {}              # (id(ib), conId) -> {"t": Ticker, "opt": contract, "last": epoch}
_OPT_CACHE = {}         # (tk,strike,right,expiry) -> resolved Option contract (explicit-expiry only)


def _ok(x):
    return x is not None and x == x and x > 0        # not None, not NaN, positive


SNAP_MAX_PTS = 2.5      # how far a loose strike may be snapped to a listed one before we refuse


def _snap_expiry(exp):
    """A weekend expiry is always wrong — US options expire on trading days. Providers write the weekend
    date routinely ("Jul 25" for the Jul 24 Friday). Roll back to the preceding Friday rather than
    returning no contract at all, which is how AAPL 325C sat unpriced and unmanaged on the board."""
    if not exp or len(str(exp)) != 8 or not str(exp).isdigit():
        return exp
    from datetime import date
    e = str(exp)
    try:
        d = date(int(e[:4]), int(e[4:6]), int(e[6:]))
    except ValueError:
        return exp
    if d.weekday() < 5:                       # Mon-Fri: already a trading day
        return exp
    d = d.fromordinal(d.toordinal() - (d.weekday() - 4))   # Sat->Fri, Sun->Fri
    return d.strftime("%Y%m%d")


def _resolve_option(ib, tk, strike, r, expiry):
    """Resolve+qualify the standard OCC contract once; cache explicit-expiry results (conId is stable)."""
    key = (tk, str(strike), r, expiry or "")
    c = _OPT_CACHE.get(key)
    if c is not None:
        return c
    from ib_async import Option
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y%m%d")
    exch = "CBOE" if tk in ("SPX", "SPXW", "VIX", "NDX", "RUT") else "SMART"
    tclass = "SPXW" if tk == "SPX" else ""
    # PROVIDERS WRITE LOOSE CONTRACTS, AND THE DESK MUST STILL PRICE THEM. Two real misses found on the
    # board, both silently unpriceable for days: prince's "NVDA 212C" (NVDA lists 210/212.5 — 212 is not a
    # strike) and prince_small's "AAPL 325C Jul 25" (Jul 25 2026 is a SATURDAY; the contract is Jul 24).
    # Neither is a parse error — it is how humans type. Snapping here fixes every call site at once.
    expiry = _snap_expiry(expiry)
    base = Option(tk, expiry or "", float(strike), r, exchange=exch, tradingClass=tclass)
    cds = [cd.contract for cd in ib.reqContractDetails(base)]
    if not cds:
        # Exact strike is not listed -> ask for the whole expiry and take the NEAREST listed strike.
        # Bounded: only accept a snap within SNAP_MAX_PTS, so "NVDA 212" -> 212.5 is fine but a
        # badly-parsed strike is still refused rather than quietly repriced onto a different trade.
        try:
            probe = Option(tk, expiry or "", 0.0, r, exchange=exch, tradingClass=tclass)
            allc = [cd.contract for cd in ib.reqContractDetails(probe)]
        except Exception:
            allc = []
        want = float(strike)
        near = [c for c in allc if abs(c.strike - want) <= SNAP_MAX_PTS]
        if not near:
            return None
        snapped = min(near, key=lambda c: (abs(c.strike - want), c.strike))
        cds = [c for c in allc if c.strike == snapped.strike]
    exp = expiry
    if not exp:
        exps = sorted(e for e in {c.lastTradeDateOrContractMonth for c in cds} if e >= today)
        if not exps:
            return None
        exp = exps[0]
    cands = [c for c in cds if c.lastTradeDateOrContractMonth == exp]
    opt = _pick_standard(cands, tk) or (cands[0] if cands else None)
    if opt is not None and expiry:                   # only cache when the expiry was explicit (stable)
        _OPT_CACHE[key] = opt
    return opt


def _live_ticker(ib, opt):
    """Get the persistent streaming Ticker for opt on this connection (subscribe once, then reuse)."""
    k = (id(ib), opt.conId)
    sub = _SUBS.get(k)
    if sub is None:
        t = ib.reqMktData(opt, "106", False, False)  # 106 = option model greeks
        _SUBS[k] = {"t": t, "opt": opt, "last": _time.time()}
        ib.sleep(1.2)                                 # cold subscribe: let the first ticks land
        return t
    sub["last"] = _time.time()
    return sub["t"]


def _fin(x, nd=None):
    """Finite-or-None: NaN/Inf (empty IBKR quotes) -> None, so the payload is valid browser JSON BY
    CONSTRUCTION and no downstream _scrub_nan walk is needed. Optionally round to nd places."""
    if x is None or x != x or x in (float("inf"), float("-inf")):
        return None
    return round(x, nd) if nd is not None else x


def _read_ticker(t, expiry):
    """NaN-safe read of a (streaming or snapshot) Ticker into the quote dict shape callers expect.
    Every numeric field is finite-scrubbed here (the single quote choke point) — NaN never leaves this fn."""
    bid, ask = t.bid, t.ask
    last = t.last if (t.last == t.last) else t.close
    mid = (bid + ask) / 2 if (_ok(bid) and _ok(ask)) else (last if (last == last and last) else None)
    # bid/ask go through _ok, not just _fin. IBKR signals "no quote" with -1.0 — a NUMBER, so _fin
    # (which only strips NaN/Inf) passed it straight through into every consumer and into the `path`
    # table. 51% of stored shadow ticks carry bid = -1.0, and outside RTH it is 81-100% of them, because
    # the monitor ticks 24/7. Anything that trusted those fields was scoring against a fill nobody could
    # get; a naive "use the bid" cost model built on them produced -2100% returns. None is the honest
    # value for "no quote", and every consumer already handles None because _fin has always emitted it.
    q = {"expiry": expiry, "bid": _fin(bid) if _ok(bid) else None,
         "ask": _fin(ask) if _ok(ask) else None, "last": _fin(last),
         "mid": _fin(mid, 2) if mid else None,
         "spread_pct": _fin((ask - bid) / mid * 100, 1) if (mid and _ok(ask) and _ok(bid)) else None}
    if t.modelGreeks:
        g = t.modelGreeks
        q.update(iv=_fin(g.impliedVol, 3), delta=_fin(g.delta, 3),
                 gamma=_fin(g.gamma, 4), theta=_fin(g.theta, 4), vega=_fin(g.vega, 4))
    return q


def reap_subs(ib, max_idle=90, cap=80):
    """Cancel streaming lines idle > max_idle sec + enforce a hard per-connection line cap (evict oldest)
    so we stay under IBKR's ~100-line budget as cards churn. Positions/watches are re-read every cycle so
    they stay warm; only truly-gone contracts age out."""
    now = _time.time()
    mine = [(k, v) for k, v in list(_SUBS.items()) if k[0] == id(ib)]
    dead = [(k, v) for k, v in mine if now - v["last"] > max_idle]
    if len(mine) - len(dead) > cap:                  # still over cap after idle-reap -> drop oldest live
        dead += sorted([kv for kv in mine if kv not in dead], key=lambda kv: kv[1]["last"])[:len(mine) - len(dead) - cap]
    for k, v in dead:
        try: ib.cancelMktData(v["opt"])
        except Exception: pass
        _SUBS.pop(k, None)


def option_quote(ticker, strike, right, expiry=None, ib=None):
    """Live option price + Greeks from IBKR. Loop callers (ib passed) read a PERSISTENT stream — instant
    after the first tick. One-off callers (ib=None) get a safe one-shot snapshot."""
    own = ib is None
    try:
        if own:
            ib = connect()
        tk, r = ticker.upper(), right.upper()[0]
        opt = _resolve_option(ib, tk, strike, r, expiry)
        if opt is None:
            return {"error": "contract not found at IBKR"}
        exp = opt.lastTradeDateOrContractMonth
        # If we snapped, SAY SO. A quote for a contract other than the one on the card is exactly the
        # failure that produced a fabricated +3391% peak; the difference must travel with the price.
        _snap = None
        try:
            if abs(float(opt.strike) - float(strike)) > 1e-9:
                _snap = f"strike {strike} -> {opt.strike:g}"
            if expiry and exp != str(expiry):
                _snap = (_snap + "; " if _snap else "") + f"expiry {expiry} -> {exp}"
        except Exception:
            pass
        if own:                                       # transient connection -> snapshot, then clean up
            t = ib.reqMktData(opt, "106", False, False)
            ib.sleep(2.0)
            q = _read_ticker(t, exp)
            try: ib.cancelMktData(opt)
            except Exception: pass
            if _snap and isinstance(q, dict): q["snapped"] = _snap
            return q
        t = _live_ticker(ib, opt)                     # persistent stream (pooled)
        q = _read_ticker(t, exp)
        if q.get("mid") is None:                      # cold sub not populated yet -> brief wait, re-read
            for _ in range(2):
                ib.sleep(0.7)
                q = _read_ticker(t, exp)
                if q.get("mid") is not None:
                    break
        if _snap and isinstance(q, dict): q["snapped"] = _snap
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
    # VIX TERM STRUCTURE: VIX9D/VIX3M/VIX1D are CBOE INDEXES, not stocks. Requesting them on SMART as
    # Stock returned "Unknown contract", so the term-structure slope (backwardation = seller danger)
    # silently stayed None in the regime log.
    if t in ("SPX", "VIX", "NDX", "RUT", "VIX9D", "VIX3M", "VIX1D", "VVIX"):
        return Index(t, "CBOE", "USD")
    if t in ("SPY", "QQQ", "IWM", "DIA"):
        return Stock(t, "ARCA", "USD")
    return Stock(t, "SMART", "USD")


_STK_CACHE = {}         # ticker -> qualified Stock/Index contract (conId is stable)
_EXP_CACHE = {}         # ticker -> {"day": YYYYMMDD, "exps": [sorted listed expirations >= today]}


def list_expiries(ib, ticker):
    """Sorted list of an option root's LISTED expirations (YYYYMMDD >= today), cached per ticker with a
    DAILY TTL — expiries only roll across days. One reqSecDefOptParams def-request (NOT a streaming line,
    so it doesn't touch the reap_subs 80-line budget). Fail-open -> keep yesterday's list, else []."""
    today = _et_today()
    tk = ticker.upper()
    c = _EXP_CACHE.get(tk)
    if c and c["day"] == today:
        return c["exps"]
    exps = (c or {}).get("exps", [])
    try:
        stk = _STK_CACHE.get(tk)
        if stk is None:
            stk = _contract(tk); ib.qualifyContracts(stk); _STK_CACHE[tk] = stk
        chains = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
        s = set()
        for ch in chains:                                   # union expirations across exchanges
            s |= set(getattr(ch, "expirations", None) or [])
        got = sorted(e for e in s if e >= today)
        if got:
            exps = got                                      # only replace on a good read
    except Exception:
        pass                                                # keep prior list rather than go blank
    _EXP_CACHE[tk] = {"day": today, "exps": exps}
    return exps


def _chain_strikes(ib, ticker, expiry=None):
    """LISTED strikes for an option root (union across exchanges), cached with the expiries. Used to
    resolve a signal that names a ticker + right but no strike — we snap to what actually trades,
    never to a number we invented."""
    tk = ticker.upper()
    try:
        stk = _STK_CACHE.get(tk)
        if stk is None:
            stk = _contract(tk); ib.qualifyContracts(stk); _STK_CACHE[tk] = stk
        chains = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
        s = set()
        for ch in chains:
            s |= set(getattr(ch, "strikes", None) or [])
        return sorted(float(x) for x in s if x and x > 0)
    except Exception:
        return []


def resolve_candidates(ticker, right, expiry=None, target=None, ib=None, n=1):
    """RESOLVE BEFORE DISCARDING. A message names a ticker and a side but no strike — the desk used to
    drop it. Instead, snap to the strikes that actually trade: the one nearest the live underlying
    (ATM), and, if a price TARGET was stated, the one nearest that. Returns quoted candidates for a
    one-tap human confirm — this NEVER auto-cards; it only makes the question specific.

    [{strike, right, expiry, mid, bid, ask, delta, kind}]  ·  [] on any failure (fail-open)."""
    own = ib is None
    try:
        if ib is None:
            ib = connect()
        tk, r = ticker.upper(), right.upper()[0]
        exp = _snap_expiry(expiry)
        if not exp:                                   # need a concrete expiry to quote against
            exps = list_expiries(ib, tk)
            exp = exps[0] if exps else None
        if not exp:
            return []
        spot = spot_quote(tk, ib=ib)          # reuse THIS connection; never spawn a nested one
        strikes = _chain_strikes(ib, tk, exp)
        if not strikes:                       # reqSecDefOptParams returns nothing on a cold line's first
            try: ib.sleep(0.8)                # touch — one short re-ask before giving up, so a warm-enough
            except Exception: pass            # caller is not defeated by a single empty def-request
            strikes = _chain_strikes(ib, tk, exp)
        if not strikes:
            return []
        wants = []                                    # (level, kind) pairs to snap
        if spot:
            wants.append((spot, "ATM"))
        if target:
            try:
                tv = float(target)
                if not spot or abs(tv - spot) > 1e-9:
                    wants.append((tv, "target"))
            except (TypeError, ValueError):
                pass
        seen, out = set(), []
        for level, kind in wants:
            k = min(strikes, key=lambda x: abs(x - level))
            if k in seen:
                continue
            seen.add(k)
            q = option_quote(tk, str(int(k)) if abs(k - int(k)) < 1e-9 else f"{k:g}", r,
                             expiry=exp, ib=ib)
            if not q or q.get("mid") is None:
                continue
            out.append({"strike": k, "right": r, "expiry": exp, "kind": kind,
                        "mid": q.get("mid"), "bid": q.get("bid"), "ask": q.get("ask"),
                        "delta": q.get("delta"), "spot": spot})
        return out[:max(1, n + 1)]
    except Exception:
        return []
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass


def _et_today():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=-4))).strftime("%Y%m%d")


def _spot_px(t):
    p = t.last if (t.last == t.last and t.last and t.last > 0) else (t.close if (t.close == t.close and t.close) else None)
    if p is None and _ok(t.bid) and _ok(t.ask):
        p = (t.bid + t.ask) / 2
    return round(p, 2) if p else None


def spot_quote(ticker, ib=None):
    """Live UNDERLYING price for stop-watching + card display. Loop callers read a PERSISTENT stream
    (instant, no per-read sleep) so the stop poll can tighten; one-off callers get a safe snapshot."""
    own = ib is None
    try:
        if ib is None:
            ib = connect()
        tk = ticker.upper()
        c = _STK_CACHE.get(tk)
        if c is None:
            c = _contract(tk); ib.qualifyContracts(c); _STK_CACHE[tk] = c
        if own:
            t = ib.reqMktData(c, "", False, False)
            ib.sleep(1.0)
            px = _spot_px(t)
            try: ib.cancelMktData(c)
            except Exception: pass
            return px
        t = _live_ticker(ib, c)                        # persistent stream (pooled with options)
        px = _spot_px(t)
        if px is None:                                 # cold sub -> brief wait, re-read
            ib.sleep(0.6); px = _spot_px(t)
        return px
    except Exception:
        return None
    finally:
        if own and ib is not None:
            try: ib.disconnect()
            except Exception: pass


def get_bars(ib, ticker, duration="2 D", size="5 mins"):
    # 2 D, not 1 D. IBKR's "1 D" returns only TODAY's bars, so at 09:48 ET there were FOUR 5-min bars and
    # indicators() (which needs >=20) returned None -- meaning every provider card read "tape read pending"
    # for roughly the first 100 minutes of EVERY session, which is exactly when most callouts arrive.
    # chip already had this fix (_chip_indicators pulls 2 D "so chip can read tape from the OPEN, not
    # ~20 min in"); the provider path never got it. indicators() now session-anchors VWAP, so the extra
    # prior-day history cannot contaminate the VWAP read.
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
    # VWAP must be anchored to TODAY even when the caller hands us multi-day bars. Indicators need prior-day
    # history to exist at all before ~20 bars have printed, but a VWAP averaged across two sessions is
    # simply a wrong number -- and every "price vs VWAP" read downstream would inherit it.
    _m = None
    try:
        _today = bars[-1].date.date()
        _m = np.array([getattr(b, "date", None) is not None and b.date.date() == _today for b in bars], bool)
        if not _m.any():
            _m = None
    except Exception:
        _m = None
    if _m is not None and v[_m].sum() > 0:
        vwap = float((tp[_m] * v[_m]).sum() / v[_m].sum())
    else:
        vwap = float((tp * v).sum() / v.sum()) if v.sum() > 0 else float(c[-1])
    sma20, sd20 = float(c[-20:].mean()), float(c[-20:].std())
    out = {"price": round(float(c[-1]), 2), "vwap": round(vwap, 2),
           "rsi": _rsi(c), "ema9": round(_ema(c[-30:], 9), 2), "ema21": round(_ema(c[-40:], 21), 2),
           "bb_up": round(sma20 + 2 * sd20, 2), "bb_mid": round(sma20, 2), "bb_low": round(sma20 - 2 * sd20, 2)}
    # LONG-LOOKBACK trend (chip only; absent on the 5-min human path, which never has this many bars).
    # The longest thing chip could previously see was EMA21 ~ 40 min — SHORTER THAN ITS OWN 60-min hold —
    # so ~30% of its entries were counter-trend pops into a horizon where SPY/QQQ measurably mean-revert
    # (variance ratio 0.835 at 60 min, z = -21.9). On 1-min bars these spans are the 15-minute EMA9/EMA21
    # (15x9=135, 15x21=315): measured 95.5% identical selection, so a coarser bar adds nothing but lag.
    # Lookback = 3x span keeps the truncated-seed residual under 1e-3.
    # float() not just round(): _ema over a numpy slice returns np.float64, which json.dumps only survives
    # via default=str -- silently emitting "708.63" as a STRING into the payload the dashboard reads.
    if len(c) >= 405:
        out["ema135"] = round(float(_ema(c[-405:], 135)), 2)
    if len(c) >= 945:
        out["ema315"] = round(float(_ema(c[-945:], 315)), 2)
    # MARKET STRUCTURE (swings, BOS, displacement, premium/discount, FVG) — the price-action lens the
    # indicators above are blind to. Computed here because this is where the raw OHLC lives; folded under
    # `struct` so it rides along wherever `ind` goes (entry-feature capture, tape-align) without touching
    # any existing field. Fail-soft inside structure.features.
    try:
        import structure as _ST
        # PYTHON floats, not the numpy array `c` — else disp_atr/premium_pct come out np.float64, which
        # json.dumps only survives via default=str (silently stringifying the numbers in the saved card).
        out["struct"] = _ST.features([float(b.high) for b in bars],
                                     [float(b.low) for b in bars], [float(x) for x in c])
    except Exception:
        out["struct"] = None
    return out


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


def context(ticker, sig_type, ib=None, strike=None, expiry=None, want_equity=False):
    """One-shot: connect (if needed), pull bars, return indicators + technical + option read + realized vol.
    `expiry` (yyyymmdd) prices the SIGNAL'S contract — without it the nearest/0DTE is used (wrong for 7/9 etc.).
    want_equity=False: fetching the balance on EVERY signal piles up account-summary subscriptions
    (IBKR Error 322) and is unused unless EQUITY_SOURCE=ibkr."""
    own = ib is None
    try:
        if own: ib = connect()
        eq = equity(ib) if want_equity else None          # live balance (only when compounding off IBKR)
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
