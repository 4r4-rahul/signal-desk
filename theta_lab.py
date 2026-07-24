#!/usr/bin/env python3
"""
theta_lab.py — the theta-sign experiment. Does COLLECTING premium beat PAYING it, on the same tape?

ARM 0 (this file, today) — the FREE retroactive counterfactual the SME insisted we run first.
chip already logs bid/ask every minute of every scalp. So "what if we had SOLD the exact contract
chip bought, same entry, same exit, same 20-minute window" is a pure REPLAY — no new trades, no
waiting. It is the cleanest possible isolation of the theta SIGN: same instrument, same direction,
same clock, opposite side. Everything else is held constant, so the only thing that differs is
buy-vs-sell.

    buyer  pays the ASK to get in, sells the BID to get out   ->  pnl = exit_bid - entry_ask
    seller sells the BID to get in, pays the ASK to get out   ->  pnl = entry_bid - exit_ask

Both cross the spread (it is a cost to each side); the DIFFERENCE is the theta+direction P&L. If the
option DECAYED over chip's hold (chip's usual flat-day fate), the seller collected it; if it RAN, the
seller paid for it. Per 1 contract each, cost-honest ($1.30 round-trip commission on both).

WHAT THIS ANSWERS: given chip's exact entries and 20-min horizon, would selling have beaten buying?
WHAT IT DOES NOT: whether a STANDALONE seller strategy is profitable (that is the forward books,
Arm 1/Arm 2) — a different question, because those change direction/moneyness/cadence too.

HONESTY: the day is the block. Scalps within a day share one tape, so the real sample is DAYS, not
trades. With only a few days logged this is a directional PREVIEW, not a verdict; it gains power daily.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import db as DB

ET = timezone(timedelta(hours=-4))
COMM = 1.30                  # round-trip commission per contract, same as the buyer book
HOLD_SEC = 1200              # chip's 20-min window — the seller holds the identical horizon


def _et_day(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _scalp_pnls(path, t1, stop):
    """One scalp -> (buyer_$pnl, seller_$pnl) per 1 contract, or None if unusable.

    Exit tick = the SAME point for both sides (identical horizon): the first tick whose MID move
    reaches +t1 or -stop (chip's single-exit discipline), else the last tick inside the 20-min window.
    Buyer and seller are then priced off that one tick's two-sided quote, opposite sides of the spread."""
    ticks = [(ts, mid, pct, bid, ask) for (ts, mid, pct, bid, ask) in path
             if bid is not None and ask is not None and bid > 0 and ask > 0]
    if len(ticks) < 2:
        return None
    t0 = datetime.fromisoformat(ticks[0][0].replace("Z", "+00:00"))
    ticks = [x for x in ticks
             if (datetime.fromisoformat(x[0].replace("Z", "+00:00")) - t0).total_seconds() <= HOLD_SEC]
    if len(ticks) < 2:
        return None
    entry_bid, entry_ask = ticks[0][3], ticks[0][4]
    ex = len(ticks) - 1                              # default: the 20-min bell (last tick)
    for i, (_, _, pct, _, _) in enumerate(ticks):
        if i == 0:
            continue
        if pct is not None and (pct >= t1 or pct <= -abs(stop)):
            ex = i
            break
    exit_bid, exit_ask = ticks[ex][3], ticks[ex][4]
    buyer = (exit_bid - entry_ask) * 100 - COMM      # bought the ask, sold the bid
    seller = (entry_bid - exit_ask) * 100 - COMM     # sold the bid, bought back the ask
    return round(buyer, 2), round(seller, 2)


def arm0(con=None):
    """Replay every chip scalp as BOTH a buy and a short of the same contract. Returns a report dict."""
    own = con is None
    if own:
        con = sqlite3.connect("file:" + str(DB.DB_PATH) + "?mode=ro", uri=True)
    try:
        scalps = con.execute(
            "SELECT o.signal_id, s.t1_pct, s.stop_pct, o.opened_utc, o.closed_utc "
            "FROM outcomes o JOIN signals s ON s.signal_id = o.signal_id "
            "WHERE s.provider='chip' AND o.kind='SHADOW' "
            "ORDER BY COALESCE(o.opened_utc, o.signal_id)").fetchall()
        per_trade, by_day = [], defaultdict(lambda: {"buyer": 0.0, "seller": 0.0, "n": 0})
        for sid, t1, stop, opened, closed in scalps:
            path = con.execute(
                "SELECT ts_utc, opt_mid, opt_pct, bid, ask FROM path "
                "WHERE signal_id=? AND phase='shadow' ORDER BY ts_utc", (sid,)).fetchall()
            r = _scalp_pnls(path, float(t1 or 30), float(stop or 30))
            if r is None:
                continue
            buyer, seller = r
            day = _et_day(closed or opened) or "?"
            per_trade.append({"sid": sid, "day": day, "buyer": buyer, "seller": seller,
                              "diff": round(seller - buyer, 2)})
            by_day[day]["buyer"] += buyer
            by_day[day]["seller"] += seller
            by_day[day]["n"] += 1
        n = len(per_trade)
        if not n:
            return {"n": 0, "note": "no replayable scalps with two-sided paths yet"}
        tot_buyer = round(sum(t["buyer"] for t in per_trade), 2)
        tot_seller = round(sum(t["seller"] for t in per_trade), 2)
        # PAIRED at trade level (both see the identical contract/exit): the diff is the clean estimator.
        diffs = [t["diff"] for t in per_trade]
        seller_wins = sum(1 for t in per_trade if t["seller"] > t["buyer"])
        # PAIRED at DAY level (the honest block): daily (seller - buyer)
        day_rows = [{"day": d, "buyer": round(v["buyer"], 2), "seller": round(v["seller"], 2),
                     "diff": round(v["seller"] - v["buyer"], 2), "n": v["n"]}
                    for d, v in sorted(by_day.items())]
        return {
            "n": n, "days": len(by_day),
            "buyer_total": tot_buyer, "seller_total": tot_seller,
            "buyer_per_ct": round(tot_buyer / n, 2), "seller_per_ct": round(tot_seller / n, 2),
            "edge_per_ct": round((tot_seller - tot_buyer) / n, 2),      # seller minus buyer, per contract
            "seller_win_rate": round(100.0 * seller_wins / n, 1),
            "mean_trade_diff": round(sum(diffs) / n, 2),
            "by_day": day_rows,
            "note": ("PREVIEW — the day is the statistical block; with %d day(s) logged this is "
                     "directional, not a verdict. Gains power daily." % len(by_day)),
        }
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    r = arm0()
    if not r.get("n"):
        print(r.get("note")); raise SystemExit
    print("ARM 0 — sell-the-same-contract counterfactual (per 1 contract, cost-honest)")
    print(f"  scalps replayed: {r['n']}  over {r['days']} day(s)")
    print(f"  BUYER  (chip, pays theta):   ${r['buyer_total']:+,.2f}   (${r['buyer_per_ct']:+.2f}/contract)")
    print(f"  SELLER (short, collects θ):  ${r['seller_total']:+,.2f}   (${r['seller_per_ct']:+.2f}/contract)")
    print(f"  ---> seller EDGE over buyer: ${r['edge_per_ct']:+.2f}/contract   "
          f"(seller beat buyer on {r['seller_win_rate']}% of trades)")
    print(f"  by day (seller - buyer):")
    for d in r["by_day"]:
        print(f"     {d['day']}  buyer ${d['buyer']:+8.2f}  seller ${d['seller']:+8.2f}  "
              f"diff ${d['diff']:+8.2f}  n={d['n']}")
    print(f"  {r['note']}")
