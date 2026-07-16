#!/usr/bin/env python3
"""
Disciplined execution gateway for tastytrade — the "smart gate, not fast trigger."

The desk is the ONLY door: every order must pass gate_order() BEFORE it can reach the broker. The gate
enforces the Desk Rules the trader committed to (2026-07-10, after a -$5,471 tilt day) — 1% size cap,
daily-loss lock, junk-setup block, and a tilt breaker (trade count, revenge re-entries, post-loss
cooldown, no sizing-up-after-a-loss). If ANY rule fails, the order is REFUSED. This is the enforcement
tastytrade's raw UI doesn't have.

The tastytrade connection (OAuth, read balances/positions, place orders) is at the bottom; it needs the
user's TT_SECRET + TT_REFRESH in .env. The GATE is broker-independent and fully testable without creds.
"""
import os
from dataclasses import dataclass, field

# ---- Desk Rules (the committed contract; tune here) ----
MAX_RISK_PCT = 1.0           # DEFAULT per-trade cap; per-provider override via OrderCtx.max_risk_pct (JMT=3%)
DAILY_LOSS_LIMIT_PCT = 6.0   # lock the desk at -6% realized (= 2 full JMT losses at 3% each)
MAX_TRADES_PER_DAY = 6        # overtrading breaker (you took ~40 on the blow-up day)
MAX_REENTRIES_PER_NAME = 2    # revenge-trading breaker (MU 6x today)
LOSS_COOLDOWN_MIN = 5         # forced pause after a losing close — no instant re-fire on tilt


@dataclass
class OrderCtx:
    """Everything the gate needs to judge a proposed order."""
    ticker: str
    risk_usd: float                       # $ at risk on this order (premium x qty x100, or to-stop)
    account_equity: float                 # REAL tastytrade NetLiq
    day_realized_usd: float = 0.0         # realized P&L so far today (negative = down)
    trades_today: int = 0                 # count of orders already placed today
    reentries_today: int = 0              # times THIS ticker already traded today
    mins_since_last_loss: float = 999.0   # minutes since the last losing close
    last_trade_was_loss: bool = False     # was the immediately prior close a loss?
    scaling_up: bool = False              # is this bigger than the prior trade's size?
    junk_block: bool = False              # did the junk gate BLOCK this setup?
    junk_reasons: list = field(default_factory=list)
    max_risk_pct: float = MAX_RISK_PCT    # per-provider cap (JMT=3%, others=1%)
    qty: int = 0                          # contracts on this order
    max_contracts: int = 0                # per-provider contract cap (JMT=3); 0 = no cap


def gate_order(ctx: OrderCtx):
    """Return {allow: bool, reasons: [...], warnings: [...]}. allow=False means the desk REFUSES to
    send it. Reasons are hard stops; warnings are shown but don't block."""
    reasons, warnings = [], []

    # 1) JUNK — hard-blocked setups never reach the broker
    if ctx.junk_block:
        reasons.append("🚫 junk setup — " + ("; ".join(ctx.junk_reasons) or "negative expectancy"))

    # 2) SIZE CAP — the single rule that makes a -36% day impossible
    cap = ctx.account_equity * ctx.max_risk_pct / 100.0
    if ctx.risk_usd > cap + 0.01:
        reasons.append(f"💰 size cap: risks ${ctx.risk_usd:,.0f} > {ctx.max_risk_pct:.0f}% limit (${cap:,.0f}). "
                       f"Cut the contracts.")
    if ctx.max_contracts and ctx.qty > ctx.max_contracts:
        reasons.append(f"📦 contract cap: {ctx.qty} > max {ctx.max_contracts} contracts. Cut it down.")

    # 3) DAILY LOSS LIMIT — lock the day, no clicking through
    loss_limit = -abs(ctx.account_equity * DAILY_LOSS_LIMIT_PCT / 100.0)
    if ctx.day_realized_usd <= loss_limit:
        reasons.append(f"🛑 daily loss limit hit ({ctx.day_realized_usd:+,.0f} ≤ {loss_limit:,.0f}). "
                       f"Desk locked for today — this is the rule that saves the account.")

    # 4) TILT BREAKER — pattern, not just P&L
    if ctx.trades_today >= MAX_TRADES_PER_DAY:
        reasons.append(f"⏳ overtrading: {ctx.trades_today} trades today (max {MAX_TRADES_PER_DAY}). "
                       f"Step away — the edge is in the FEW, not the many.")
    if ctx.reentries_today >= MAX_REENTRIES_PER_NAME:
        reasons.append(f"🔁 revenge guard: already traded {ctx.ticker} {ctx.reentries_today}x today "
                       f"(max {MAX_REENTRIES_PER_NAME}). One bad read shouldn't become six losses.")
    if ctx.last_trade_was_loss and ctx.mins_since_last_loss < LOSS_COOLDOWN_MIN:
        reasons.append(f"❄️ cooldown: {ctx.mins_since_last_loss:.0f} min since a loss "
                       f"(need {LOSS_COOLDOWN_MIN}). Don't fire on tilt.")
    if ctx.last_trade_was_loss and ctx.scaling_up:
        reasons.append("📈 sizing UP right after a loss — the classic tilt tell. Blocked.")

    return {"allow": len(reasons) == 0, "reasons": reasons, "warnings": warnings}


# =========================  tastytrade connection (needs TT_SECRET + TT_REFRESH in .env)  =========================
def _load_env():
    """Populate TT_* from .env (the desk reads .env per-call, not into the environment)."""
    from pathlib import Path
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            if k.startswith("TT_") and not os.environ.get(k):
                os.environ[k] = v


def _session():
    """OAuth session from env. v13 uses provider_secret + refresh_token (set up once on tastytrade's
    developer portal). Async under the hood — callers wrap with asyncio.run()."""
    _load_env()
    from tastytrade import Session
    secret, refresh = os.environ.get("TT_SECRET"), os.environ.get("TT_REFRESH")
    if not secret or not refresh:
        raise RuntimeError("tastytrade not configured — set TT_SECRET and TT_REFRESH in .env")
    is_test = os.environ.get("TT_TEST", "1") == "1"      # sandbox by default until proven, then flip to live
    return Session(provider_secret=secret, refresh_token=refresh, is_test=is_test)


async def _live_account_async():
    from tastytrade import Account
    async with _session() as sess:
        accts = await Account.get(sess)
        if isinstance(accts, (list, tuple)):
            if not accts:
                return {"error": "connected OK, but no trading account in this sandbox — "
                                 "add one at developer.tastytrade.com (Customer Info → Add New Account)"}
            acct = accts[0]
        else:
            acct = accts
        bal = await acct.get_balances(sess)
        pos = await acct.get_positions(sess)
        return {"account": getattr(acct, "account_number", "?"),
                "net_liq": float(bal.net_liquidating_value), "cash": float(bal.cash_balance),
                "positions": [{"symbol": p.symbol, "qty": float(p.quantity),
                               "avg": float(p.average_open_price)} for p in pos]}


def live_account():
    """Sync wrapper — real NetLiq + open positions from tastytrade. Returns None if not configured."""
    import asyncio
    try:
        return asyncio.run(_live_account_async())
    except Exception as e:
        return {"error": str(e)}


import time as _time
_ACCT_CACHE = {"t": 0.0, "data": None}


def account_snapshot(max_age=30):
    """Cached account read (balance + positions) for the desk — refreshes at most every max_age seconds."""
    if _time.time() - _ACCT_CACHE["t"] > max_age:
        _ACCT_CACHE["data"] = live_account()
        _ACCT_CACHE["t"] = _time.time()
    return _ACCT_CACHE["data"]


def _occ(ticker, yyyymmdd, cp, strike):
    """Build the OCC option symbol, e.g. ('SPY','20260710','C',756) -> 'SPY   260710C00756000'."""
    return f"{ticker.upper().ljust(6)}{yyyymmdd[2:]}{cp.upper()[0]}{int(round(float(strike) * 1000)):08d}"


async def _submit_async(occ, action, qty, price, dry_run):
    from tastytrade import Account
    from tastytrade.order import NewOrder, OrderType, OrderTimeInForce, OrderAction
    from tastytrade.instruments import Option
    from decimal import Decimal
    amap = {"BTO": OrderAction.BUY_TO_OPEN, "STC": OrderAction.SELL_TO_CLOSE,
            "BTC": OrderAction.BUY_TO_CLOSE, "STO": OrderAction.SELL_TO_OPEN}
    async with _session() as sess:
        acct = (await Account.get(sess))[0]
        opt = await Option.get(sess, occ)
        leg = opt.build_leg(Decimal(str(qty)), amap[action])
        signed = -abs(Decimal(str(price))) if action in ("BTO", "BTC") else abs(Decimal(str(price)))  # debit=neg
        order = NewOrder(time_in_force=OrderTimeInForce.DAY, order_type=OrderType.LIMIT,
                         legs=[leg], price=signed)
        resp = await acct.place_order(sess, order, dry_run=dry_run)
        po = getattr(resp, "order", None)
        return {"order_id": getattr(po, "id", None), "status": str(getattr(po, "status", "") or ""),
                "bp_effect": float(getattr(getattr(resp, "buying_power_effect", None),
                                           "change_in_buying_power", 0) or 0),
                "warnings": [str(getattr(w, "message", w)) for w in (getattr(resp, "warnings", []) or [])]}


def submit_order(ctx, occ_parts, action, qty, price, dry_run=True):
    """GATE FIRST — only if the Desk Rules pass does the order reach tastytrade. occ_parts =
    (ticker, yyyymmdd, C/P, strike). dry_run=True validates without placing. Returns the full outcome."""
    gate = gate_order(ctx)
    if not gate["allow"]:
        return {"allow": False, "placed": False, "reasons": gate["reasons"]}
    occ = _occ(*occ_parts)
    import asyncio
    try:
        r = asyncio.run(_submit_async(occ, action, qty, price, dry_run))
        return {"allow": True, "placed": not dry_run, "dry_run": dry_run, "occ": occ,
                "warnings": gate["warnings"], **r}
    except Exception as e:
        return {"allow": True, "placed": False, "occ": occ, "error": str(e)}


if __name__ == "__main__":
    # smoke-test the GATE (no broker needed)
    eq = 9516
    tests = [
        ("clean 1% trade", OrderCtx("SPY", 90, eq, day_realized_usd=-50, trades_today=2)),
        ("oversized", OrderCtx("SPX", 1500, eq)),
        ("day locked", OrderCtx("SPY", 90, eq, day_realized_usd=-300)),
        ("overtrading", OrderCtx("MU", 90, eq, trades_today=6)),
        ("revenge", OrderCtx("MU", 90, eq, reentries_today=2)),
        ("junk", OrderCtx("SNDK", 90, eq, junk_block=True, junk_reasons=["far-OTM Δ0.06"])),
        ("tilt cooldown", OrderCtx("SPY", 90, eq, last_trade_was_loss=True, mins_since_last_loss=1)),
    ]
    for label, ctx in tests:
        r = gate_order(ctx)
        print(f"  {label:16} -> {'✅ ALLOW' if r['allow'] else '🚫 REFUSE'}  {r['reasons']}")
