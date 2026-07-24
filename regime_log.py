#!/usr/bin/env python3
"""
regime_log.py — ONE row per trading day: the volatility-regime state that every stratified analysis
joins against. The SME's "don't waste 30 days" item: without this, we cannot ask whether chip's bleed
(or the seller's edge) is IV-conditional, and both books stay blind to the one thing that actually
predicts a premium seller's P&L — the variance risk premium (IV vs realized vol).

Per ticker per day it captures the SELLER'S WORLD:
  · atm_iv          — the price of what a seller sells (annualized implied vol)
  · rv              — trailing realized vol (annualized), from intraday bars
  · vrp = iv - rv   — THE variance risk premium. Positive & wide => selling is favored; negative =>
                      the market is realizing more than it's pricing, selling is -EV. Single most
                      predictive daily feature for whether collecting theta pays.
  · iv_rank / iv_pct — where today's IV sits in its trailing 1-year range (needs history -> we log
                      daily from day one so the rank has a lookback as fast as possible)
  · vix, vix_slope  — the macro fear gauge + term-structure sign (backwardation = seller danger)
  · exp_move        — the day's expected move (IV * spot * sqrt(t/252)); strikes get placed in EM units
  · label           — trend / chop, from the EMA135/315 separation chip already computes
  · event           — FOMC / CPI / NFP / OPEX flag (sellers die on event days; must be stratifiable)

Everything is FAIL-SOFT to None: a missing IBKR quote never throws, it just leaves that cell empty for
the day. Storage is an append/upsert JSON keyed by date, retained forever (owner rule: nothing deleted).
"""
import json
import math
from datetime import datetime, timezone, timedelta

import deskpaths as _P

ET = timezone(timedelta(hours=-4))
STORE = _P.CHANNELS / "_regime_log.json"
IV_HIST_STORE = _P.CHANNELS / "_iv_history.json"   # ticker -> [{"day","atm_iv"}] for the IV-rank lookback
TRADING_DAYS = 252
FLAT_SEP_PCT = 0.05                                 # |EMA135-EMA315|/price below this = a flat/chop day


def _et_today():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _load(path, default):
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def _save(path, obj):
    try:
        from safe_io import safe_write_json
        safe_write_json(path, obj, min_keep_ratio=0.0)
    except Exception:
        pass


RTH_HOURS = 6.5             # a full trading session = one 1/252 trading day


def expected_move(spot, atm_iv, trading_hours_left):
    """Expected 1-sigma move in POINTS over the trading hours remaining: spot * iv * sqrt(t_years),
    where t_years scales by TRADING time (vol accrues in market hours, not wall-clock). A full session
    (6.5h) = 1/252 year, so a full 0DTE day's EM = spot*iv*sqrt(1/252). iv is annualized (e.g. 0.15)."""
    if not spot or not atm_iv or not trading_hours_left or trading_hours_left <= 0:
        return None
    t_years = trading_hours_left / (RTH_HOURS * TRADING_DAYS)
    return round(spot * atm_iv * math.sqrt(t_years), 2)


def iv_rank(ticker, atm_iv, hist=None):
    """Where today's ATM IV sits in its trailing ~1-year range. Returns (rank_pct, percentile) or
    (None, None) until there is enough history. rank = (iv-min)/(max-min); percentile = % of days below."""
    if atm_iv is None:
        return None, None
    hist = hist if hist is not None else _load(IV_HIST_STORE, {})
    vals = [h["atm_iv"] for h in hist.get(ticker.upper(), []) if h.get("atm_iv") is not None][-TRADING_DAYS:]
    if len(vals) < 20:                                # too little history to be meaningful yet
        return None, None
    lo, hi = min(vals), max(vals)
    rank = round(100.0 * (atm_iv - lo) / (hi - lo), 1) if hi > lo else None
    pct = round(100.0 * sum(1 for v in vals if v < atm_iv) / len(vals), 1)
    return rank, pct


def _note_iv(ticker, atm_iv, day):
    """Record today's ATM IV to the rolling history, ONCE per day, PINNED to the FIRST reading of the
    session. The decision-relevant IV for a 0DTE seller is at the open/entry, not the last-write-of-day
    near the close (0DTE IV crushes into the bell). So we keep the first non-null reading and never
    overwrite it — otherwise the whole IV-rank history would be built on near-close IV (SME SHOULD-FIX)."""
    if atm_iv is None:
        return
    hist = _load(IV_HIST_STORE, {})
    row = hist.setdefault(ticker.upper(), [])
    if row and row[-1].get("day") == day:
        return                                        # already pinned today's open IV — do NOT overwrite
    row.append({"day": day, "atm_iv": atm_iv})
    hist[ticker.upper()] = row[-(TRADING_DAYS + 20):]  # bounded
    _save(IV_HIST_STORE, hist)


# ---- EVENT CALENDAR. Static known-event dates (extend as needed). OPEX = 3rd Friday. -------------
# Sellers die on event days; this only has to be good enough to STRATIFY them out, not to be complete.
_EVENT_DAYS = set()          # e.g. {"2026-07-30"} for an FOMC — owner/ops can extend channels/_events.json


def _gap_from_close(bars):
    """Overnight gap %: today's first bar open vs the prior session's last close. One of the best cheap
    predictors of the trend days that hurt sellers. Fail-soft to None."""
    if not bars or len(bars) < 2:
        return None
    today = bars[-1].date.date()
    today_bars = [b for b in bars if getattr(b, "date", None) and b.date.date() == today]
    prev_bars = [b for b in bars if getattr(b, "date", None) and b.date.date() < today]
    if not today_bars or not prev_bars:
        return None
    prev_close, today_open = prev_bars[-1].close, today_bars[0].open
    return round((today_open - prev_close) / prev_close * 100, 3) if prev_close else None


def _event_flag(day):
    try:
        ext = _load(_P.CHANNELS / "_events.json", {})   # {"2026-07-30":"FOMC", ...} optional override
        if day in ext:
            return ext[day]
    except Exception:
        pass
    if day in _EVENT_DAYS:
        return "event"
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        if d.weekday() == 4 and 1 <= d.day <= 7:      # NFP: first Friday of the month (computable)
            return "NFP"
        if d.weekday() == 4 and 15 <= d.day <= 21:    # OPEX: third Friday
            return "OPEX"
    except Exception:
        pass
    # FOMC / CPI / PPI are NOT formulaic — they must be maintained in channels/_events.json for the
    # study window (e.g. {"2026-07-30":"FOMC"}). Until populated, those days are UNFLAGGED — a known
    # gap the SME called out; the override mechanism above is the fix, the content is ops to supply.
    return None


def update(tickers=("SPY", "QQQ"), ib=None):
    """Compute + upsert TODAY's regime row for each ticker. Called from the desk loop (cheap, idempotent).
    Fail-soft: any missing quote leaves that field None. Returns today's row dict."""
    import market as MK
    day = _et_today()
    log = _load(STORE, {})
    row = log.get(day) or {"day": day, "tickers": {}}
    # VIX + term structure (index quotes; may be None after hours)
    vix = _safe(lambda: MK.spot_quote("VIX", ib=ib))
    vix9d = _safe(lambda: MK.spot_quote("VIX9D", ib=ib))
    vix3m = _safe(lambda: MK.spot_quote("VIX3M", ib=ib))
    slope = round(vix3m - vix, 2) if (vix and vix3m) else None      # >0 contango (calm), <0 backwardation
    row["vix"], row["vix9d"], row["vix3m"], row["vix_slope"] = vix, vix9d, vix3m, slope
    row["event"] = _event_flag(day)
    now = datetime.now(ET)
    hours_to_close = max(0.0, (16 * 60 - (now.hour * 60 + now.minute)) / 60.0)
    for tk in tickers:
        try:
            import market as MK2
            bars = _safe(lambda: MK2.get_bars(ib or MK2.connect(), tk)) if ib else _safe(lambda: MK2._bars_cached(tk))
        except Exception:
            bars = None
        rv = _safe(lambda: MK.realized_vol(bars)) if bars else None
        ind = _safe(lambda: MK.indicators(bars)) if bars else None
        spot = (ind or {}).get("price")
        # ATM IV = AVERAGE of the nearest call and put (an index surface has call!=put ATM IV; pricing
        # "what a seller sells" off one side ignores skew). Log both, and the 25-ish-delta skew.
        atm_iv = atm_c = atm_p = None
        if spot:
            k = str(int(round(spot)))
            qc = _safe(lambda: MK.option_quote(tk, k, "C", ib=ib)) or {}
            qp = _safe(lambda: MK.option_quote(tk, k, "P", ib=ib)) or {}
            atm_c, atm_p = qc.get("iv"), qp.get("iv")
            ivs = [x for x in (atm_c, atm_p) if x is not None]
            atm_iv = round(sum(ivs) / len(ivs), 3) if ivs else None
        _note_iv(tk, atm_iv, day)
        ivr, ivp = iv_rank(tk, atm_iv)
        # VRP = IV - RV, BOTH annualized decimals (market.realized_vol returns std*sqrt(78*252), a
        # fraction on the same scale as modelGreeks.impliedVol). No /100 — that was a scale bug.
        vrp = round(atm_iv - rv, 4) if (atm_iv is not None and rv is not None) else None
        # STABLE FULL-SESSION expected move (1/252yr) — one consistent daily figure the strike engine
        # consumes; the trade-time remaining-session EM is computed separately at entry.
        em = expected_move(spot, atm_iv, RTH_HOURS)
        skew = round(atm_p - atm_c, 3) if (atm_p is not None and atm_c is not None) else None  # put-call IV
        # gap from prior close: first vs last of the session-split bars (fail-soft)
        gap = _safe(lambda: _gap_from_close(bars))
        # flat/trend label from the EMA135/315 separation chip already trusts
        e135, e315 = (ind or {}).get("ema135"), (ind or {}).get("ema315")
        sep = round(abs(e135 - e315) / spot * 100, 3) if (e135 and e315 and spot) else None
        label = ("flat" if (sep is not None and sep < FLAT_SEP_PCT)
                 else "trend" if sep is not None else None)
        row["tickers"][tk] = {
            "spot": spot, "atm_iv": atm_iv, "atm_iv_call": atm_c, "atm_iv_put": atm_p, "skew": skew,
            "rv": (round(rv, 4) if rv is not None else None),
            "vrp": vrp, "iv_rank": ivr, "iv_pct": ivp, "exp_move": em, "gap_pct": gap,
            "ema_sep_pct": sep, "label": label,
        }
    log[day] = row
    _save(STORE, log)
    return row


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def today():
    return _load(STORE, {}).get(_et_today())


if __name__ == "__main__":
    # math self-test (no market needed)
    print("expected_move(690, 0.15, 6.5h) =", expected_move(690, 0.15, 6.5), "pts")
    print("iv_rank with no history      =", iv_rank("SPY", 0.15, hist={}))
    h = {"SPY": [{"day": f"d{i}", "atm_iv": 0.10 + 0.001 * i} for i in range(60)]}
    print("iv_rank(0.15) w/ 60d history  =", iv_rank("SPY", 0.15, hist=h))
    print("event flag 2026-07-17 (3rdFri)=", _event_flag("2026-07-17"))
