#!/usr/bin/env python3
"""
Discipline engine — RISK-BUDGET model (not a flat trade count).

Per the risk-manager SME: correlated 0DTE trades aren't independent bets
(10 same-direction SPX ≈ 1.4 effective bets), so we cap CORRELATED DOLLARS AT
RISK, not tickets. Reads today's tracker journals and decides if you may take more.

Units: 1R = 1% of account. Env-overridable:
  DAILY_RISK_BUDGET_R (3)   total risk you may deploy today
  MAX_HEAT_R          (2)   max risk open at once (frees as you close)
  MAX_CLUSTER_R       (1.5) max risk in one ticker+direction (correlation cap)
  DAILY_STOP_R        (-4)  realized-loss floor for the day
  MAX_CONSEC_LOSSES   (3)   consecutive losers -> done
  MAX_TICKETS         (8)   backstop count; MAX_CLUSTER_TICKETS (4) per cluster
"""
import csv, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent
ET = timezone(timedelta(hours=-4))
try:                                    # shared 1R basis — must match signal_engine's sizing stop
    from signal_engine import SIZING_STOP_FRAC as STOP_FRAC
except Exception:
    STOP_FRAC = 0.35
BUDGET_R = float(os.environ.get("DAILY_RISK_BUDGET_R", "3"))
MAX_HEAT_R = float(os.environ.get("MAX_HEAT_R", "2"))
MAX_CLUSTER_R = float(os.environ.get("MAX_CLUSTER_R", "1.5"))
DAILY_STOP_R = float(os.environ.get("DAILY_STOP_R", "-4"))
MAX_CONSEC = int(os.environ.get("MAX_CONSEC_LOSSES", "3"))
MAX_TICKETS = int(os.environ.get("MAX_TICKETS", "8"))
# green-day house money: once up this much (peak), unlock 50% of realized gains as extra
# budget; but if you give back half your peak profit, lock it and stop.
HOUSE_TRIGGER_R = float(os.environ.get("HOUSE_TRIGGER_R", "3"))
HOUSE_FRACTION = float(os.environ.get("HOUSE_FRACTION", "0.5"))
GIVEBACK_FRAC = float(os.environ.get("GIVEBACK_FRAC", "0.5"))


def today():
    return datetime.now(ET).date().isoformat()


def _risk_R(row):
    m = re.search(r"R=([\d.]+)", row.get("notes", "") or "")
    if m:
        return float(m.group(1))
    return 0.5 if row.get("lotto") == "1" else 1.0     # fallback


# US large-cap beta trades are the SAME bet (SPX≈SPY≈QQQ≈IWM, ρ~0.99) — one cluster.
BETA = {"SPX", "SPY", "QQQ", "IWM", "ES", "NDX", "SPXW", "DIA", "RUT"}


def _cluster_key(ticker, typ):
    t = (ticker or "").upper()
    return ("US-BETA", typ) if t in BETA else (t, typ)


# Per-provider sub-budgets (compose UNDER the global caps via min(); scale with robustness ranking).
# Env override: PROVIDER_CAPS_JSON='{"jmt":[2,2],...}'  -> {prov: [daily_R, max_concurrent]}
import json as _json
# [daily_R (% of acct), max_concurrent]. JMT raised 2026-07-14 to [9,3]: his per-trade size is ~3%
# (3-contract floor), so a 2R cap let ONE trade max the day and block every later signal — a config
# mismatch, not discipline. 9R/3 = up to three 3% JMT trades/day. Trusted low-freq engine; user's call.
PROVIDER_CAPS = _json.loads(os.environ.get("PROVIDER_CAPS_JSON", "{}")) or {
    "jmt": [9.0, 3], "prince": [1.0, 1], "prince_small": [1.0, 1],
    "sulker": [0.5, 1], "optionking": [0.0, 0], "mike": [0.0, 0]}


def state():
    d = today()
    tickets = 0
    deployed = 0.0            # total risk taken today (R)
    open_rows = []            # still TAKEN (not closed) -> heat
    closed = []              # (ts, return_pct)
    dep_by_prov = defaultdict(float)     # risk deployed today, per provider
    open_by_prov = defaultdict(int)      # positions still open, per provider
    for jp in ROOT.glob("channels/*/journal.csv"):
        prov = jp.parent.name
        try:
            for r in csv.DictReader(open(jp)):
                if not (r.get("alert_ts", "") or "").startswith(d):
                    continue
                st = r.get("status")
                if st in ("TAKEN", "CLOSED"):
                    tickets += 1
                    deployed += _risk_R(r)
                    dep_by_prov[prov] += _risk_R(r)
                if st == "TAKEN":
                    open_rows.append(r)
                    open_by_prov[prov] += 1
                if st == "CLOSED" and r.get("my_return_pct"):
                    closed.append((r["alert_ts"], float(r["my_return_pct"])))
        except Exception:
            continue
    provider_R = {}
    for prov, (dr, mc) in PROVIDER_CAPS.items():
        dep, opn = round(dep_by_prov.get(prov, 0.0), 2), open_by_prov.get(prov, 0)
        provider_R[prov] = {"daily_cap": dr, "deployed": dep, "concurrent_cap": mc, "open": opn,
                            "blocked": dep >= dr or opn >= mc,
                            "reason": ("daily %sR cap" % dr if dep >= dr else
                                       "%d concurrent cap" % mc if opn >= mc else None)}
    closed.sort()
    run = peak = 0.0
    for _, p in closed:
        run += p / (STOP_FRAC * 100)
        peak = max(peak, run)
    realized_R = round(run, 2)
    peak_R = round(peak, 2)
    heat = sum(_risk_R(r) for r in open_rows)

    # green-day house-money extension
    house_active = peak_R >= HOUSE_TRIGGER_R and realized_R > 0
    extra_R = round(HOUSE_FRACTION * realized_R, 2) if house_active else 0.0
    eff_budget = round(BUDGET_R + extra_R, 2)
    giveback = peak_R >= HOUSE_TRIGGER_R and realized_R <= GIVEBACK_FRAC * peak_R
    clusters = defaultdict(float)
    for r in open_rows:
        clusters[_cluster_key(r.get("ticker"), r.get("type"))] += _risk_R(r)   # correlation-aware
    max_cluster = max(clusters.values()) if clusters else 0.0
    consec = 0
    for _, p in reversed(closed):
        if p < -1:
            consec += 1
        else:
            break

    reasons = []
    if deployed >= eff_budget:
        reasons.append(f"risk budget {eff_budget}R deployed")
    if realized_R <= DAILY_STOP_R:
        reasons.append(f"daily stop hit ({realized_R:+.1f}R ≤ {DAILY_STOP_R}R)")
    if giveback:
        reasons.append(f"gave back half of +{peak_R}R peak — lock gains, done")
    if consec >= MAX_CONSEC:
        reasons.append(f"{consec} losses in a row")
    if tickets >= MAX_TICKETS:
        reasons.append(f"max {MAX_TICKETS} tickets")
    if heat >= MAX_HEAT_R:
        reasons.append(f"max heat {MAX_HEAT_R}R open — close something first")
    if max_cluster >= MAX_CLUSTER_R:
        reasons.append(f"correlated cluster at {round(max_cluster,2)}R (cap {MAX_CLUSTER_R}R) — too concentrated")

    return {"date": d, "tickets": tickets, "max_tickets": MAX_TICKETS,
            "deployed_R": round(deployed, 2), "budget_R": BUDGET_R,
            "eff_budget_R": eff_budget, "extra_R": extra_R, "house_active": house_active,
            "heat_R": round(heat, 2), "max_heat_R": MAX_HEAT_R,
            "max_cluster_R": MAX_CLUSTER_R, "cluster_R": round(max_cluster, 2),
            "realized_R": realized_R, "peak_R": peak_R, "daily_stop_R": DAILY_STOP_R,
            "consec_losses": consec, "max_consec": MAX_CONSEC, "provider_R": provider_R,
            "blocked": bool(reasons), "reasons": reasons}


if __name__ == "__main__":
    import json
    print(json.dumps(state(), indent=2))
