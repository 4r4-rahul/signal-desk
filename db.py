#!/usr/bin/env python3
"""
Signal Desk data warehouse (SQLite) — every signal, decision, and outcome, structured & queryable.

Design (SME: keep it normalized but ML-friendly):
  signals   — one row per carded signal: the FULL decision context (features) as-of card time.
  decisions — the terminal action on a signal: TAKEN / SKIPPED / WATCHED (+ size, fill).
  outcomes  — the result: REALIZED (taken -> $/R) or SHADOW (skipped -> would-be WIN/LOSS).
  ml_dataset (VIEW) — flat features + label, one row per signal, ready for pandas/CSV.

Honesty rules baked in:
  - Features are as-of DECISION time (no future leakage into the feature columns).
  - Idempotent upserts keyed on signal_id, so live updates never duplicate.
  - NULL where unknown — we never fabricate a value.
  - The .db is DERIVED from the committed jsonl (source of truth) — rebuild any time with `backfill`.

CLI:
  python3 db.py backfill      # (re)build the DB from all jsonl sources
  python3 db.py export [path] # dump ml_dataset to CSV (default ml_dataset.csv)
  python3 db.py summary       # quick counts + a peek at the ML table
"""
import json, glob, sqlite3, csv
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "signal_desk.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  ts_utc TEXT, provider TEXT, channel TEXT, msg_id TEXT,
  ticker TEXT, strike REAL, type TEXT, expiry TEXT, dte INTEGER,
  premium REAL, lotto INTEGER, watch INTEGER,
  score REAL, tier TEXT, grade TEXT, grade_score REAL, size_mult REAL, conviction REAL,
  limit_px REAL, stop_px REAL, t1 REAL, t2 REAL, t3 REAL,
  stop_pct REAL, t1_pct REAL, t2_pct REAL, t3_pct REAL,
  rr1 REAL, rr2 REAL, rr3 REAL, chase_pct REAL, wide INTEGER,
  contracts INTEGER, risk_usd REAL, risk_pct REAL, risk_r REAL, risk_basis TEXT,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  rv REAL, iv_rv REAL, omega REAL, spread_pct REAL, bid REAL, ask REAL, mid REAL,
  tape TEXT, cosign TEXT,
  verified INTEGER, paper INTEGER, manual_override INTEGER,
  raw TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  signal_id TEXT PRIMARY KEY,
  action TEXT, qty INTEGER, fill_px REAL, ts_utc TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  signal_id TEXT PRIMARY KEY,
  kind TEXT, result TEXT,
  realized_usd REAL, r_multiple REAL,
  entry_px REAL, exit_px REAL, peak_pct REAL, trough_pct REAL,
  hold_secs INTEGER, opened_utc TEXT, closed_utc TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_provider ON signals(provider);
CREATE INDEX IF NOT EXISTS ix_sig_ticker   ON signals(ticker);
CREATE INDEX IF NOT EXISTS ix_sig_ts       ON signals(ts_utc);
CREATE INDEX IF NOT EXISTS ix_dec_action   ON decisions(action);
CREATE INDEX IF NOT EXISTS ix_out_result   ON outcomes(result);
CREATE VIEW IF NOT EXISTS ml_dataset AS
SELECT s.*, d.action, d.qty, d.fill_px,
       o.kind AS outcome_kind, o.result, o.realized_usd, o.r_multiple,
       o.peak_pct, o.trough_pct, o.hold_secs,
       CASE o.result WHEN 'WIN' THEN 1 WHEN 'LOSS' THEN 0 ELSE NULL END AS label_win
FROM signals s
LEFT JOIN decisions d ON d.signal_id = s.signal_id
LEFT JOIN outcomes  o ON o.signal_id = s.signal_id;
"""


def _f(x):
    try:
        return float(str(x)) if x is not None and str(x) != "" else None
    except Exception:
        return None


def connect():
    con = sqlite3.connect(str(DB_PATH))
    con.executescript(SCHEMA)
    return con


def _upsert(con, table, row):
    row = {k: v for k, v in row.items() if v is not None or k == "signal_id"}
    if not row.get("signal_id"):
        return
    cols = list(row)
    ph = ",".join("?" for _ in cols)
    setc = ",".join(f"{c}=excluded.{c}" for c in cols if c != "signal_id")
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
           f"ON CONFLICT(signal_id) DO UPDATE SET {setc}") if setc else \
          f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    con.execute(sql, [row[c] for c in cols])


def _signal_row(e):
    """Flatten a card entry into the signals feature row (as-of decision time)."""
    sg = e.get("sig") or {}; pl = e.get("plan") or {}; tk = e.get("ticket") or {}
    o = e.get("opt") or {}; tech = e.get("tech") or {}; gr = e.get("grade") or {}
    iv, rv = o.get("iv"), tech.get("rv")
    st = pl.get("stop_terms") or {}
    return {
        "signal_id": e.get("id"), "ts_utc": e.get("ts_utc") or e.get("iso"),
        "provider": e.get("provider"), "channel": e.get("channel"), "msg_id": e.get("msg_id"),
        "ticker": sg.get("ticker"), "strike": _f(sg.get("strike")), "type": sg.get("type"),
        "expiry": sg.get("expiry"), "dte": sg.get("dte"), "premium": _f(sg.get("premium")),
        "lotto": 1 if sg.get("lotto") else 0, "watch": 1 if (sg.get("watch") or e.get("watch")) else 0,
        "score": e.get("score"), "tier": e.get("tier_key"),
        "grade": gr.get("g"), "grade_score": gr.get("score"),
        "size_mult": e.get("size_mult"), "conviction": e.get("conviction"),
        "limit_px": tk.get("limit"), "stop_px": tk.get("stop"),
        "t1": tk.get("t1"), "t2": tk.get("t2"), "t3": tk.get("t3"),
        "stop_pct": pl.get("stop_pct") or tk.get("stop_pct"),
        "t1_pct": tk.get("t1_pct"), "t2_pct": tk.get("t2_pct"), "t3_pct": tk.get("t3_pct"),
        "rr1": tk.get("rr1"), "rr2": tk.get("rr2"), "rr3": tk.get("rr3"),
        "chase_pct": tk.get("chasing_pct"), "wide": 1 if tk.get("wide") else 0,
        "contracts": pl.get("contracts"), "risk_usd": pl.get("risk"), "risk_pct": pl.get("risk_pct"),
        "risk_r": pl.get("risk_R"), "risk_basis": pl.get("risk_basis"),
        "iv": iv, "delta": o.get("delta"), "gamma": o.get("gamma"), "theta": o.get("theta"), "vega": o.get("vega"),
        "rv": rv, "iv_rv": (round(iv / rv, 3) if (iv and rv) else None), "omega": st.get("Omega"),
        "spread_pct": o.get("spread_pct") or tk.get("spread_pct"),
        "bid": o.get("bid"), "ask": o.get("ask"), "mid": o.get("mid"),
        "tape": tech.get("label"),
        "cosign": ",".join(e.get("cosign")) if e.get("cosign") else None,
        "verified": (1 if e.get("verified") else (0 if e.get("verified") is False else None)),
        "paper": 1 if e.get("paper") else 0, "manual_override": 1 if e.get("manual_override") else 0,
        "raw": (e.get("raw") or "")[:300],
    }


# ---------------- live capture (called from server) ----------------
def record_signal(entry):
    try:
        con = connect(); _upsert(con, "signals", _signal_row(entry))
        con.commit(); con.close()
    except Exception:
        pass


def record_decision(signal_id, action, qty=None, fill_px=None, ts_utc=None):
    try:
        con = connect()
        _upsert(con, "decisions", {"signal_id": signal_id, "action": action,
                                   "qty": qty, "fill_px": _f(fill_px), "ts_utc": ts_utc})
        con.commit(); con.close()
    except Exception:
        pass


def _shadow_outcome(e):
    sh = e.get("shadow") or {}
    if sh.get("state") not in ("WIN", "LOSS"):
        return None
    return {"signal_id": e.get("id"), "kind": "SHADOW", "result": sh["state"],
            "realized_usd": None, "r_multiple": None,
            "entry_px": sh.get("ref"), "exit_px": None,
            "peak_pct": sh.get("peak"), "trough_pct": sh.get("trough"),
            "hold_secs": None, "opened_utc": sh.get("start_utc"), "closed_utc": None}


def sync_outcomes(signals=None):
    """Upsert REALIZED outcomes from closed_trades.jsonl + SHADOW outcomes from live signals.
    Cheap + idempotent — safe to call every monitor cycle."""
    try:
        con = connect()
        for f in glob.glob(str(ROOT / "channels" / "*" / "closed_trades.jsonl")):
            for ln in Path(f).read_text().splitlines():
                if not ln.strip():
                    continue
                r = json.loads(ln)
                sid = r.get("journal_id") or r.get("id")
                if not sid:
                    continue
                rz = r.get("realized")
                res = "WIN" if (rz or 0) > 0 else ("LOSS" if (rz or 0) < 0 else "SCRATCH")
                _upsert(con, "outcomes", {
                    "signal_id": sid, "kind": "REALIZED", "result": res,
                    "realized_usd": _f(rz), "r_multiple": _f(r.get("R_multiple")),
                    "entry_px": _f(r.get("avg_entry")), "exit_px": _f(r.get("avg_exit")),
                    "peak_pct": _f(r.get("peak_pct")), "hold_secs": r.get("hold_secs"),
                    "opened_utc": r.get("opened_utc"), "closed_utc": r.get("closed_utc")})
        for e in (signals or []):
            row = _shadow_outcome(e)
            if row:
                # don't overwrite a REALIZED outcome with a shadow (taken beats shadow)
                cur = con.execute("SELECT kind FROM outcomes WHERE signal_id=?", (row["signal_id"],)).fetchone()
                if not cur or cur[0] == "SHADOW":
                    _upsert(con, "outcomes", row)
        con.commit(); con.close()
    except Exception:
        pass


# ---------------- backfill / export / summary ----------------
def backfill():
    con = connect()
    seen = 0
    # 1) current cards
    fp = ROOT / "channels" / "_signals.json"
    entries = json.loads(fp.read_text()) if fp.exists() else []
    # 2) archived signals (full card history)
    for f in glob.glob(str(ROOT / "channels" / "*" / "live_signals.jsonl")):
        for ln in Path(f).read_text().splitlines():
            if ln.strip():
                entries.append(json.loads(ln))
    for e in entries:
        if not e.get("id"):
            continue
        _upsert(con, "signals", _signal_row(e))
        st = e.get("status")
        act = {"TAKEN": "TAKEN", "CLOSED": "TAKEN", "CUT": "TAKEN",
               "SKIPPED": "SKIPPED", "WATCHING": "WATCHED"}.get(st)
        if act:
            _upsert(con, "decisions", {"signal_id": e["id"], "action": act, "ts_utc": e.get("ts_utc")})
        row = _shadow_outcome(e)
        if row:
            _upsert(con, "outcomes", row)
        seen += 1
    con.commit(); con.close()
    sync_outcomes(entries)     # realized outcomes from closed_trades
    return seen


def export_csv(path="ml_dataset.csv"):
    con = connect()
    cur = con.execute("SELECT * FROM ml_dataset")
    cols = [d[0] for d in cur.description]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(cols); w.writerows(cur.fetchall())
    n = con.execute("SELECT COUNT(*) FROM ml_dataset").fetchone()[0]
    con.close()
    return n, path


def summary():
    con = connect()
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    n_sig = q("SELECT COUNT(*) FROM signals")
    n_dec = q("SELECT COUNT(*) FROM decisions")
    taken = q("SELECT COUNT(*) FROM decisions WHERE action=?", "TAKEN")
    skipped = q("SELECT COUNT(*) FROM decisions WHERE action=?", "SKIPPED")
    n_out = q("SELECT COUNT(*) FROM outcomes")
    wins = q("SELECT COUNT(*) FROM outcomes WHERE result=?", "WIN")
    loss = q("SELECT COUNT(*) FROM outcomes WHERE result=?", "LOSS")
    labeled = q("SELECT COUNT(*) FROM ml_dataset WHERE label_win IS NOT NULL")
    print(f"  signals  : {n_sig}")
    print(f"  decisions: {n_dec}  (taken {taken}, skipped {skipped})")
    print(f"  outcomes : {n_out}  (win {wins}, loss {loss})")
    print(f"  ml rows w/ label: {labeled}")
    print("\n  by provider (signals / labeled):")
    for prov, n, lab in con.execute(
            "SELECT s.provider, COUNT(*), COUNT(m.label_win) FROM signals s "
            "LEFT JOIN ml_dataset m ON m.signal_id=s.signal_id GROUP BY s.provider ORDER BY 2 DESC"):
        print(f"    {str(prov):<13} {n:>4} / {lab or 0}")
    con.close()


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "backfill":
        print(f"  backfilled {backfill()} signals -> {DB_PATH.name}")
        summary()
    elif cmd == "export":
        n, p = export_csv(sys.argv[2] if len(sys.argv) > 2 else "ml_dataset.csv")
        print(f"  exported {n} rows -> {p}")
    else:
        summary()
