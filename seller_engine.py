#!/usr/bin/env python3
"""
seller_engine.py — the PURE math of the premium-seller books. No IO, no market, no state, so every
rule here is unit-testable against a synthetic option chain. seller_desk.py drives it live; seller_book.py
scores it; this file just answers "given a chain, what spread, at what credit, how big, and when to close."

TWO ARMS (per the SME, to un-confound the theta A/B):
  · ARM 1 — DIRECTIONAL credit spread on chip's EXACT EMA side. chip's gate says up -> chip buys calls ->
            the mirror SELLS a PUT credit spread (bullish, collects theta, same directional view). Down ->
            SELL a CALL credit spread. This isolates the theta SIGN with direction held equal to chip.
  · ARM 2 — NEUTRAL 0DTE iron condor: a call spread AND a put spread, ~16-delta shorts. Pure short-vol;
            profits when the market stays in a range. Tests "am I a profitable neutral seller."

CONVENTIONS, cost-honest:
  · Sell a spread => collect the SHORT's bid, pay the LONG's ask (cross the spread the way a real fill does).
    credit = short_bid - long_ask.  Buy it back => pay short_ask, collect long_bid.
  · defined_risk per contract = (width - credit) * 100.  Max loss is bounded by the wing.
  · commission = $1.30 PER CONTRACT PER LEG (a vertical = 2 legs open; a condor = 4). Round trip doubles it.
  · size to RISK_PCT of equity on the DEFINED risk (not premium): qty = floor(budget / (defined_risk*100)).
"""

COMM_PER_LEG = 0.65         # $ per contract per leg one-way ($1.30 round-trip/leg — matches the buyer book)
RISK_PCT = 1.0              # % of equity risked on defined risk per position
DEFAULT_TARGET_DELTA = 0.16
DEFAULT_WIDTH = 5.0
TAKE_PROFIT_FRAC = 0.50     # close at 50% of credit captured (standard mechanical seller)
# Refuse a spread whose ROUND-TRIP exit cost eats this share of the credit. 0.35 is a judgement call,
# not a measured optimum, and is deliberately loose enough to admit a tight day on a single name while
# still rejecting the 10%-wide quotes that motivated keeping the seller on SPY/QQQ. Every refusal is
# logged with its ratio, so after ~30 sessions this threshold can be replaced by evidence.
MAX_FRICTION_RATIO = 0.35


def _nearest_by_delta(chain, right, target_delta):
    """The listed strike on `right` whose |delta| is closest to target. chain rows: {strike,right,delta,
    bid,ask,mid}. Returns the row or None."""
    cands = [c for c in chain if (c.get("right") or "").upper() == right.upper()
             and c.get("delta") is not None and c.get("bid") is not None and c.get("ask") is not None]
    if not cands:
        return None
    return min(cands, key=lambda c: abs(abs(c["delta"]) - target_delta))


def _leg(chain, right, strike):
    for c in chain:
        if (c.get("right") or "").upper() == right.upper() and abs(c.get("strike", -1) - strike) < 1e-6:
            return c
    return None


def build_vertical(chain, right, target_delta=DEFAULT_TARGET_DELTA, width=DEFAULT_WIDTH):
    """A short credit spread on one side: sell ~target-delta, buy `width` further OTM (the wing).
    right='C' -> bearish call spread; right='P' -> bullish put spread. Returns a spread dict or None."""
    short = _nearest_by_delta(chain, right, target_delta)
    if not short:
        return None
    # the long wing is `width` further OTM: higher strike for calls, lower for puts
    long_strike = short["strike"] + width if right.upper() == "C" else short["strike"] - width
    long = _leg(chain, right, long_strike)
    if not long or long.get("bid") is None or long.get("ask") is None:
        # The exact wing may not be in the captured chain (a $5 wing beyond a 16-delta short can sit
        # below the ladder's delta floor). Prefer a strike at/BEYOND the requested wing; if none exists,
        # fall back to the FURTHEST available strike beyond the short — a NARROWER spread is still a
        # valid defined-risk position, and real_width below records what we actually got. Returning None
        # here is what made every condor unbuildable at the live open.
        is_c = right.upper() == "C"
        beyond = [c for c in chain if (c.get("right") or "").upper() == right.upper()
                  and c.get("bid") is not None and c.get("ask") is not None
                  and ((c["strike"] > short["strike"]) if is_c else (c["strike"] < short["strike"]))]
        if not beyond:
            return None
        at_or_past = [c for c in beyond
                      if ((c["strike"] >= long_strike) if is_c else (c["strike"] <= long_strike))]
        long = (min(at_or_past, key=lambda c: abs(c["strike"] - long_strike)) if at_or_past
                else max(beyond, key=lambda c: abs(c["strike"] - short["strike"])))
    credit = round(short["bid"] - long["ask"], 2)                 # sell short bid, buy long ask
    real_width = abs(long["strike"] - short["strike"])
    defined_risk = round(real_width - credit, 2)                  # per share; *100 for the contract
    if credit <= 0 or defined_risk <= 0:
        return None                                              # no credit / inverted -> not tradeable
    # EXIT FRICTION — the cost that decides whether a wide-quoted name is sellable at all.
    # The ENTRY crossing is already inside `credit` (short bid, long ask). The exit is not: closing
    # means buying the short back at its ASK and selling the wing at its BID, so the round trip pays
    # both leg spreads again. On SPY/QQQ that is pennies against the credit. On a single name quoting
    # 10% wide it can exceed the entire credit collected, which is the real content of "you do not sell
    # 0DTE credit spreads into a 10% spread" — it was previously enforced by never listing those
    # tickers at all. Now it is measured per trade, so a name is refused on its ACTUAL quote that day
    # rather than on its reputation, and a tight day on NVDA is allowed to trade.
    # UNITS: leg quotes are PER SHARE, commission is PER CONTRACT — so the commission has to be divided
    # by 100 to sit in the same units as the credit. Mixing them made a 3-cent SPY spread look like it
    # cost twice its own credit to exit, which would have refused every trade on the desk.
    sp_short = (short.get("ask") or 0) - (short.get("bid") or 0)
    sp_long = (long.get("ask") or 0) - (long.get("bid") or 0)
    exit_friction = round(max(0.0, sp_short) + max(0.0, sp_long) + 2 * COMM_PER_LEG / 100.0, 4)
    return {"right": right.upper(), "short_strike": short["strike"], "long_strike": long["strike"],
            "width": real_width, "credit": credit, "defined_risk": defined_risk,
            "short_delta": short["delta"], "short_iv": short.get("iv"),
            "credit_to_width": round(credit / real_width, 3),
            "exit_friction": exit_friction,
            "friction_ratio": round(exit_friction / credit, 3) if credit > 0 else None,
            "legs": 2}


def build_condor(chain, target_delta=DEFAULT_TARGET_DELTA, width=DEFAULT_WIDTH):
    """Iron condor = a call vertical (bearish) + a put vertical (bullish). Neutral short-vol."""
    call = build_vertical(chain, "C", target_delta, width)
    put = build_vertical(chain, "P", target_delta, width)
    if not call or not put:
        return None
    credit = round(call["credit"] + put["credit"], 2)
    # for a condor, only ONE side can be breached at expiry -> max loss = worst wing - total credit
    defined_risk = round(max(call["width"], put["width"]) - credit, 2)
    return {"kind": "condor", "call": call, "put": put, "credit": credit,
            "defined_risk": defined_risk, "width": max(call["width"], put["width"]),
            "credit_to_width": round(credit / max(call["width"], put["width"]), 3), "legs": 4}


def directional(chain, chip_side, target_delta=DEFAULT_TARGET_DELTA, width=DEFAULT_WIDTH):
    """ARM 1: the credit spread that mirrors chip's directional view.
    chip buys CALLS (bullish) -> we SELL a PUT spread (bullish, collect theta).
    chip buys PUTS  (bearish) -> we SELL a CALL spread (bearish, collect theta)."""
    right = "P" if (chip_side or "").upper().startswith("C") else "C"
    v = build_vertical(chain, right, target_delta, width)
    if v:
        v["kind"] = "vertical"
        v["mirrors_chip_side"] = chip_side
    return v


def size(equity, defined_risk_per_ct, risk_pct=RISK_PCT):
    """Contracts by DEFINED risk. defined_risk_per_ct is per-share; *100 is the per-contract max loss.
    Returns (qty, risk_usd, note). qty 0 => the single-contract max loss exceeds the budget."""
    if not defined_risk_per_ct or defined_risk_per_ct <= 0:
        return 0, None, "no defined risk"
    per_ct = defined_risk_per_ct * 100.0
    budget = equity * (risk_pct / 100.0)
    qty = int(budget // per_ct)
    if qty < 1:
        return 0, round(per_ct, 2), f"${per_ct:,.0f}/ct max loss exceeds ${budget:,.0f} budget"
    return qty, round(qty * per_ct, 2), f"{qty} ct x ${per_ct:,.0f} risk = ${qty*per_ct:,.0f} ({risk_pct}%)"


def spread_buyback(short_ask, long_bid):
    """Cost to CLOSE a vertical: buy back the short (pay ask), sell the long (collect bid)."""
    if short_ask is None or long_bid is None:
        return None
    return round(short_ask - long_bid, 2)


def manage(credit, buyback_cost, minutes_to_close, legs, take_frac=TAKE_PROFIT_FRAC):
    """The seller's management rule, on ONE mark. Returns (action, realized_pnl_per_ct, reason).
      · take profit when buyback_cost has decayed to <= (1-take_frac)*credit  (captured >= take_frac)
      · EOD flatten at the close (avoid pin/assignment) — books the current mark
      · else hold. Defined risk is the wing, so we do NOT run a hard 2x stop (SME: it converts
        eventual-expire-worthless winners into realized losses on gappy 0DTE).
    P&L per contract (per share*100) = (credit - exit_cost)*100 - commission_round_trip."""
    rt_comm = COMM_PER_LEG * 2 * legs                            # open + close, per leg
    if buyback_cost is None:
        return "hold", None, "no mark"
    captured = credit - buyback_cost                            # per share
    if captured >= take_frac * credit:
        pnl = round(captured * 100 - rt_comm, 2)
        return "take_profit", pnl, f"captured {captured/credit*100:.0f}% of credit"
    if minutes_to_close is not None and minutes_to_close <= 1:
        pnl = round(captured * 100 - rt_comm, 2)
        return "eod", pnl, "expire/flatten at the close"
    return "hold", round(captured * 100 - rt_comm, 2), "open"


def open_cost(legs):
    """Commission to OPEN (one-way, all legs) — for recording the entry."""
    return round(COMM_PER_LEG * legs, 2)
