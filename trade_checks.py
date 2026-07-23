#!/usr/bin/env python3
"""
trade_checks.py — the dimensions a trade is checked on BEFORE entry, WHILE managed, and AFTER close.

A trade is not one decision, it is three, and each has its own failure modes. Before entry the question is
"is this instrument tradeable and is the plan executable"; while live it is "is the plan being followed and
is the thesis still true"; after close it is "did we get what the plan promised, and what did it cost".
Most of these were being checked somewhere in the desk already — but scattered, unnamed, and invisible.
Naming them is what turns a P&L into a diagnosis.

Every check returns one of: ok · warn · fail · n/a. `fail` at the PRE stage means the trade should not be
taken. `warn` never blocks — it is a note for the post-mortem. Nothing here computes market data; it reads
what the desk already produced, so it cannot disagree with the card.
"""

OK, WARN, FAIL, NA = "ok", "warn", "fail", "n/a"


def _c(key, label, state, detail=""):
    return {"key": key, "label": label, "state": state, "detail": detail}


def pre_trade(*, quote=None, mech=None, plan=None, qty=None, risk_usd=None, equity=None,
              probs=None, extension=None, htf=None, news=None, session_min=None, ladder=None):
    """Everything that must be true BEFORE money is committed."""
    q, m, p = quote or {}, mech or {}, probs or {}
    out = []

    # 1. the instrument itself
    mid, ask, delta = q.get("mid"), q.get("ask"), q.get("delta")
    spread, smax = q.get("spread_pct"), m.get("spread_max")
    out.append(_c("contract", "Contract resolved", OK if mid else FAIL,
                  f"mid ${mid}" if mid else "no quote — cannot price"))
    if spread is not None and smax:
        out.append(_c("spread", "Spread inside cap",
                      OK if spread <= smax else FAIL, f"{spread}% vs {smax}% cap"))
    if delta is not None:
        ad = abs(delta)
        out.append(_c("delta", "Delta in band", OK if 0.40 <= ad <= 0.85 else WARN, f"Δ{ad:.2f}"))
    fuse, fstate = (p.get("stop_theta_min"), p.get("fuse_state"))
    if fstate == "never":
        out.append(_c("fuse", "Theta fuse", OK, "stop sits below intrinsic — decay can never reach it"))
    elif fuse is not None:
        out.append(_c("fuse", "Theta fuse", OK if fuse >= 40 else WARN, f"{fuse}m of decay to the stop"))

    # 2. can we size it, and is the plan EXECUTABLE
    if qty is not None:
        out.append(_c("size", "Position sized", OK if qty >= 1 else FAIL,
                      f"{qty} contract{'s' if qty != 1 else ''}"
                      + (f" · ${risk_usd:,.0f} at risk" if risk_usd else "")
                      + (f" of ${equity*0.01:,.0f} budget" if equity else "")))
    if ladder:
        deg = ladder.get("degraded")
        out.append(_c("ladder", "Exit plan executable", WARN if deg else OK, ladder.get("note", "")))
        t = ladder.get("tests") or {}
        if t:
            miss = [k.upper() for k in ("t1", "t2", "t3") if not t.get(k)]
            out.append(_c("targets", "Tests T1/T2/T3", WARN if miss else OK,
                          ("untested: " + ", ".join(miss)) if miss else "all three legs exercised"))

    # 3. is there room for the move the plan needs
    if p.get("req_now_pts") is not None:
        out.append(_c("reach", "T1 reachable", OK, f"needs {p['req_now_pts']} pts of underlying"))
    if p.get("t1_first") is not None:
        v = p["t1_first"]
        out.append(_c("race", "T1 before stop", OK if v >= 0.5 else WARN, f"{v*100:.0f}% (σ-free)"))
    if extension and extension.get("verdict") not in (None, "unknown"):
        v = extension["verdict"]
        out.append(_c("runway", "Room in the range", WARN if v in ("unreachable", "extended") else OK,
                      extension.get("note") or v))

    # 4. context
    if htf and htf.get("agrees") is not None:
        out.append(_c("trend", "With the ~5h trend", OK if htf["agrees"] else FAIL,
                      "EMA135/315 agree" if htf["agrees"] else "against the higher trend"))
    if news and news not in ("none", None):
        out.append(_c("news", "Event risk", WARN, str(news)))
    if session_min is not None:
        out.append(_c("session", "Session timing", OK, f"{int(session_min)}m since the open"))
    return out


def in_trade(*, life=None, plan=None, probs=None, live_read=None):
    """What is true WHILE the position is on. The question here is discipline, not selection."""
    L, out = life or {}, []
    now, peak, trough = L.get("now_pct"), L.get("peak_pct"), L.get("trough_pct")
    hit = L.get("hit") or [False, False, False]

    out.append(_c("pnl", "Open P&L", OK if (now or 0) >= 0 else WARN,
                  f"{now:+.0f}% · peak {peak:+.0f}% · trough {trough:+.0f}%"
                  if now is not None else "—"))
    legs = [("T1", hit[0]), ("T2", hit[1]), ("T3", hit[2])]
    done = [k for k, v in legs if v]
    out.append(_c("legs", "Scale-out progress", OK if done else NA,
                  (" · ".join(done) + " filled") if done else "no target hit yet"))
    if hit[0]:
        out.append(_c("be", "Stop at breakeven", OK, "moved after T1 — the position cannot lose now"))
    cl = L.get("clock") or {}
    if cl.get("mins_close") is not None:
        out.append(_c("clock", "Time left", OK if cl["mins_close"] > 30 else WARN,
                      f"{cl['mins_close']}m of decay remaining"))
    th = L.get("thesis") or {}
    if th.get("state"):
        out.append(_c("thesis", "Entry thesis", OK if th["state"] in ("intact", "strong") else WARN,
                      th.get("why") or th["state"]))
    gb = L.get("give_back") or {}
    if gb.get("level"):
        out.append(_c("giveback", "Give-back from peak", WARN if gb["level"] != "none" else OK,
                      gb.get("why") or gb["level"]))
    return out


def post_trade(*, row=None, life=None):
    """What we learn AFTER it closes. This is where a P&L becomes a diagnosis."""
    r, L, out = row or {}, life or {}, []
    pnl, qty, ret = r.get("pnl"), r.get("qty"), r.get("return_pct")
    out.append(_c("result", "Realised", OK if (pnl or 0) > 0 else FAIL,
                  f"${pnl:+,.2f} on {qty} contract{'s' if qty != 1 else ''} ({ret:+.1f}%)"
                  if pnl is not None else "—"))
    if r.get("r") is not None:
        out.append(_c("r", "R multiple", OK if r["r"] > 0 else FAIL, f"{r['r']:+.2f}R"))
    peak = r.get("peak_pct")
    if peak is not None and ret is not None:
        left = peak - ret
        out.append(_c("vs_peak", "Exit vs peak", OK if left <= 10 else WARN,
                      f"peaked {peak:+.0f}%, exited {ret:+.0f}% — left {left:.0f}pp on the table"))
    if r.get("exec_shape"):
        out.append(_c("plan_run", "Plan actually run", WARN if r.get("ladder_degraded") else OK,
                      r.get("exec_plan", "")))
    if r.get("hold_min") is not None:
        out.append(_c("hold", "Hold time", OK, f"{r['hold_min']:.0f} minutes"))
    er = L.get("end_reason") or r.get("result")
    if er:
        out.append(_c("why", "Exit reason", OK, str(er)))
    # what the round trip cost, as a share of what was made or lost
    if r.get("entry") and qty:
        cost = 1.30 * qty + (r["entry"] * 100 * qty) * 0.0075 * 2
        out.append(_c("cost", "Round-trip cost", WARN if abs(pnl or 0) < cost * 2 else OK,
                      f"≈${cost:,.0f} of friction"
                      + (f" vs ${abs(pnl):,.0f} moved" if pnl else "")))
    return out


def summarise(checks):
    n = len(checks)
    fails = [c for c in checks if c["state"] == FAIL]
    warns = [c for c in checks if c["state"] == WARN]
    return {"n": n, "ok": n - len(fails) - len(warns), "warn": len(warns), "fail": len(fails),
            "blocking": [c["label"] for c in fails]}
