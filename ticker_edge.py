#!/usr/bin/env python3
"""
ticker_edge.py — per-ticker edge, BUY side vs SELL side, at day / week / month granularity.

The two theta books answer "does buying or selling premium win overall." This answers the sharper
question the owner asked: WHICH TICKERS win, on WHICH side. A name can be a good thing to sell premium on
(tight, range-bound) and a bad thing to buy (chops through your stop), or the reverse — and the whole
universe was chosen without knowing which. This is the scoreboard that tells you, per ticker:

    n · win% · avg win $ · avg loss $        for BUY (chip) and SELL (seller), each period.

Sourced from the books' own resolved trades — chip_book.trades_all() (uncapped) and seller_book — so the
dollars match the $250k books exactly. A trade counts in a period bucket by the day it CLOSED. "win" is
pnl > 0, "loss" is pnl < 0; scratches (exactly 0) are rare and counted in n but neither win nor loss.
Read-only.
"""
from collections import defaultdict
from datetime import datetime


def _period_key(day, kind):
    """day 'YYYY-MM-DD' -> its bucket label for the granularity. week is ISO year-Www."""
    if not day or len(day) < 10:
        return "?"
    if kind == "month":
        return day[:7]
    if kind == "week":
        try:
            y, w, _ = datetime.strptime(day, "%Y-%m-%d").isocalendar()
            return f"{y}-W{w:02d}"
        except Exception:
            return "?"
    return day            # kind == "day"


def _stats(pnls):
    """win/loss stats for one bucket of per-trade dollar P&Ls."""
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_pct": round(100.0 * len(wins) / n, 1) if n else None,
        "loss_pct": round(100.0 * len(losses) / n, 1) if n else None,
        "avg_win": round(sum(wins) / len(wins)) if wins else None,
        "avg_loss": round(sum(losses) / len(losses)) if losses else None,
        "net": round(sum(pnls)),
    }


def _by_ticker_period(trades):
    """{granularity: {period: {ticker: stats}}} from a list of trades with ticker/pnl/day-or-closed."""
    out = {}
    for kind in ("day", "week", "month"):
        buckets = defaultdict(lambda: defaultdict(list))
        for t in trades:
            day = (t.get("closed") or t.get("day") or "")[:10]
            tk = t.get("ticker")
            if not day or not tk:
                continue
            buckets[_period_key(day, kind)][tk].append(float(t.get("pnl") or 0))
        out[kind] = {per: {tk: _stats(pnls) for tk, pnls in tks.items()}
                     for per, tks in buckets.items()}
    return out


def edge():
    """The full matrix, both sides, all three granularities, plus the list of periods available per
    granularity (newest first) so the UI can offer a period picker."""
    try:
        import chip_book as CB
        buy = CB.trades_all()
    except Exception:
        buy = []
    try:
        import seller_book as SB
        sell = SB._load()
    except Exception:
        sell = []
    buy_m = _by_ticker_period(buy)
    sell_m = _by_ticker_period(sell)
    periods = {}
    for kind in ("day", "week", "month"):
        keys = set(buy_m.get(kind, {})) | set(sell_m.get(kind, {}))
        periods[kind] = sorted((k for k in keys if k != "?"), reverse=True)
    # universe order: chip's list if importable, else whatever traded
    try:
        import chip_desk as CD
        universe = list(CD.TICKERS)
    except Exception:
        universe = sorted({t.get("ticker") for t in buy + sell if t.get("ticker")})
    return {"universe": universe, "periods": periods, "buy": buy_m, "sell": sell_m}


if __name__ == "__main__":
    e = edge()
    print("universe:", e["universe"])
    for kind in ("day", "week", "month"):
        print(f"\n=== {kind} ===  periods: {e['periods'][kind]}")
        for side in ("buy", "sell"):
            latest = e["periods"][kind][0] if e["periods"][kind] else None
            if not latest:
                continue
            print(f"  {side} @ {latest}:")
            for tk, s in sorted(e[side][kind].get(latest, {}).items()):
                print(f"    {tk:6s} n={s['n']:2d} win {str(s['win_pct']):>5}%  "
                      f"avgW {str(s['avg_win']):>6}  avgL {str(s['avg_loss']):>6}  net {s['net']:+}")
