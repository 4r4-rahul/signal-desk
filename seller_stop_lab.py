#!/usr/bin/env python3
"""
seller_stop_lab.py — should the seller books run a stop-loss? Answer it from REPLAY, not opinion.

The seller currently has NO hard stop: defined risk is the wing, and on gappy 0DTE a stop converts
expire-worthless winners into realized losses (the SME reason in seller_engine.manage). But that is an
argument for a LOOSE stop, not for none — a name that trends through the short strike and STAYS there
eats the full width, and closing at 2.5x the credit early would cap it. Which effect dominates is an
empirical question, and this book is paper with minute-by-minute marks, so we can just measure it.

METHOD. For every RESOLVED seller position we replay its own buyback path from the ladder (which pins
open-position legs into every snapshot for exactly this reason), minute by minute over [opened, closed].
For each candidate stop we find the FIRST minute the loss reached stop_mult x credit and book the exit
there; if it never did, the trade's actual outcome stands. Then compare {no stop, 2x, 2.5x, 3x} on total
P&L, win rate, and — the number that matters for a tail strategy — the WORST single trade.

HONEST LIMITS:
  · Only positions with per-leg strikes AND an expiry recorded can be replayed (both captured recently),
    and only where the ladder actually logged those legs over the hold. Older trades are skipped, loudly.
  · A stop matters only on LOSERS. With a run of all-winners it will (correctly) report "no difference" —
    which is the true current state, not a bug. The value accrues the first bad day.

Run:  python3 seller_stop_lab.py
"""
import json
import sqlite3
from collections import defaultdict

import deskpaths as _P

STORE = _P.CHANNELS / "seller" / "positions.jsonl"
STOP_GRID = (None, 2.0, 2.5, 3.0)      # None = no stop (the current default)
COMM_PER_LEG = 0.65


def _load():
    try:
        return [json.loads(l) for l in STORE.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def _ladder_con():
    import ladder_logger as LL
    return sqlite3.connect("file:" + str(LL.LADDER_DB) + "?mode=ro", uri=True)


def _buyback_path(con, ticker, expiry, legs, t0, t1):
    """Minute-by-minute buyback cost of the spread over [t0, t1], from the ladder.
    legs = [(right, short_strike, long_strike), ...] (one for a vertical, two for a condor).
    buyback of a vertical = short_ask - long_bid; a condor sums its two. Returns [(ts, buyback), ...]."""
    # pull every snapshot ts in the window, then the two legs' quotes at each
    tss = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_utc FROM ladder WHERE ticker=? AND expiry=? AND ts_utc>=? AND ts_utc<=? "
        "ORDER BY ts_utc", (ticker, expiry, t0, t1))]
    out = []
    for ts in tss:
        total, ok = 0.0, True
        for (right, ks, kl) in legs:
            s = con.execute("SELECT ask FROM ladder WHERE ticker=? AND expiry=? AND ts_utc=? AND right=? "
                            "AND abs(strike-?)<1e-6", (ticker, expiry, ts, right, ks)).fetchone()
            l = con.execute("SELECT bid FROM ladder WHERE ticker=? AND expiry=? AND ts_utc=? AND right=? "
                            "AND abs(strike-?)<1e-6", (ticker, expiry, ts, right, kl)).fetchone()
            if not s or not l or s[0] is None or l[0] is None:
                ok = False
                break
            total += (s[0] - l[0])          # buy short back at ask, sell wing at bid
        if ok:
            out.append((ts, round(total, 3)))
    return out


def _legs_of(p):
    sp = p.get("spread") or {}
    if p.get("kind") == "condor":
        c, pu = sp.get("call") or {}, sp.get("put") or {}
        if c.get("short_strike") is None or pu.get("short_strike") is None:
            return None
        return [("C", c["short_strike"], c["long_strike"]), ("P", pu["short_strike"], pu["long_strike"])]
    if sp.get("short_strike") is None:
        return None
    return [(sp.get("right"), sp["short_strike"], sp["long_strike"])]


def _exit_under_stop(path, credit, stop_mult, qty, legs_n, actual_pnl):
    """P&L if this stop had been active. Stop fires at the first minute buyback >= (1+mult)*credit."""
    rt = COMM_PER_LEG * 2 * legs_n
    if stop_mult is None:
        return actual_pnl                                  # no stop -> the trade's real outcome
    thresh = (1 + stop_mult) * credit
    for ts, bb in path:
        if bb >= thresh:
            return round(((credit - bb) * 100 - rt) * qty, 2)   # realized loss at the stop
    return actual_pnl                                      # never triggered -> real outcome


def run():
    rows = _load()
    replayable = [p for p in rows if _legs_of(p) and p.get("expiry") and p.get("opened") and p.get("closed")]
    print(f"resolved seller trades: {len(rows)}   replayable (have legs+expiry+ladder): {len(replayable)}")
    losers = [p for p in rows if (p.get('pnl') or 0) < 0]
    print(f"losing trades so far: {len(losers)}  <- a stop can only ever change a LOSER\n")
    if not replayable:
        print("Nothing to replay yet: the trades on file predate per-leg/expiry capture, or the ladder\n"
              "did not log their legs. The machinery is ready; it produces an answer as new trades resolve.")
        if not losers:
            print("\nAnd with 0 losers so far, every stop level would give the IDENTICAL result regardless.")
        return
    con = _ladder_con()
    totals = defaultdict(lambda: {"pnl": 0.0, "wins": 0, "n": 0, "worst": 0.0, "fired": 0})
    per_trade = []
    for p in replayable:
        legs = _legs_of(p)
        path = _buyback_path(con, p["ticker"], p["expiry"], legs,
                             p["opened"], p["closed"])
        row = {"ticker": p["ticker"], "arm": p["arm"], "actual": p.get("pnl")}
        for sm in STOP_GRID:
            pnl = _exit_under_stop(path, p["credit"], sm, p.get("qty", 1), p.get("legs", 2), p.get("pnl") or 0)
            key = "no stop" if sm is None else f"{sm:g}x"
            t = totals[key]
            t["pnl"] += pnl; t["n"] += 1; t["worst"] = min(t["worst"], pnl)
            if pnl > 0: t["wins"] += 1
            if sm is not None and pnl != (p.get("pnl") or 0): t["fired"] += 1
            row[key] = pnl
        per_trade.append(row)
    con.close()
    print(f"{'stop':>8s} {'total P&L':>10s} {'win%':>6s} {'worst trade':>12s} {'stop fired':>11s}")
    for sm in STOP_GRID:
        key = "no stop" if sm is None else f"{sm:g}x"
        t = totals[key]
        wr = 100.0 * t["wins"] / t["n"] if t["n"] else 0
        print(f"{key:>8s} {t['pnl']:>+10.0f} {wr:>5.0f}% {t['worst']:>+12.0f} "
              f"{('—' if sm is None else str(t['fired'])+' trades'):>11s}")
    print("\nlower 'worst trade' = the stop capped a big loser; if 'total P&L' also drops, it whipsawed\n"
          "winners into losses. Choose the loosest stop that improves worst-trade without cutting total.")


if __name__ == "__main__":
    run()
