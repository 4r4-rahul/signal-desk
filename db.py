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
import json, glob, sqlite3, csv, threading, time
from pathlib import Path

ROOT = Path(__file__).parent
import deskpaths as _P
DB_PATH = _P.DB_FILE

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
  realized_usd REAL, r_multiple REAL, return_pct REAL, exit_vs_peak_pct REAL,
  prov_pct_out REAL, your_pct_out REAL,
  entry_px REAL, exit_px REAL, peak_pct REAL, trough_pct REAL,
  hold_secs INTEGER, opened_utc TEXT, closed_utc TEXT
);
CREATE TABLE IF NOT EXISTS events (
  signal_id TEXT, source TEXT, seq INTEGER, ts_utc TEXT,
  kind TEXT, qty INTEGER, price REAL, pct_out REAL, pct_up REAL,
  realized_delta REAL, stop_ref TEXT, raw TEXT,
  PRIMARY KEY (signal_id, source, seq)
);
CREATE INDEX IF NOT EXISTS ix_ev_sig  ON events(signal_id);
CREATE INDEX IF NOT EXISTS ix_ev_kind ON events(kind);
-- path: the FULL CONDITIONS at every tick through a trade's life (option + underlying + tape + momentum).
-- For future ML: learn what plays behind a peak, a reversal, a momentum shift. Rich raw; labels derived later.
CREATE TABLE IF NOT EXISTS path (
  signal_id TEXT, ts_utc TEXT, phase TEXT, mins REAL, tod_min INTEGER,
  opt_mid REAL, opt_pct REAL, bid REAL, ask REAL,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
  spot REAL, spot_pct REAL, rsi REAL, vwap REAL, ema9 REAL, ema21 REAL, bb_up REAL, bb_low REAL,
  momentum TEXT, is_peak INTEGER, is_trough INTEGER,
  PRIMARY KEY (signal_id, ts_utc)
);
CREATE INDEX IF NOT EXISTS ix_path_sig ON path(signal_id);
CREATE INDEX IF NOT EXISTS ix_path_peak ON path(is_peak);
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


# Columns added AFTER a table first shipped. `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table,
# so a new column in SCHEMA would never appear on a live DB — every read of it would raise. ALTER is the
# only migration path SQLite gives us. Append-only: never rename or drop, and keep the tuple ordered.
_ADDED_COLS = (
    # (table, column, type-and-default)
    # --- execution-cost re-scoring of SHADOW outcomes (see SHADOW_BASIS). The pre-fix MID-in/MID-out
    #     score is preserved alongside the new one so the change is auditable and reversible. ---
    ("outcomes", "r_mid",          "REAL"),   # PRE-FIX r_multiple  (mid-in / mid-out, no costs)
    ("outcomes", "return_pct_mid", "REAL"),   # PRE-FIX return_pct  (ditto)
    ("outcomes", "entry_fill_px",  "REAL"),   # the ASK we charge the shadow for the entry
    ("outcomes", "cost_pct",       "REAL"),   # commission+fees as % of premium paid
    ("outcomes", "score_basis",    "TEXT"),   # basis version + any degradation flag (see SHADOW_BASIS)
    ("outcomes", "n_tick_tradeable", "INTEGER"),  # ticks carrying a two-sided quote (exit opportunities)
)


def _ensure_cols(con):
    """Idempotent forward-migration for columns added after a table shipped. Cheap (one PRAGMA per table)
    and safe to call on every connect — SQLite has no ADD COLUMN IF NOT EXISTS, so we diff first."""
    tables = {t for t, _, _ in _ADDED_COLS}
    have = {}
    for t in tables:
        try:
            have[t] = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        except Exception:
            have[t] = set()
    for t, col, decl in _ADDED_COLS:
        if col in have.get(t, set()):
            continue
        try:
            con.execute(f"ALTER TABLE {t} ADD COLUMN {col} {decl}")
            have[t].add(col)
        except Exception:
            pass          # column raced in from another connection, or the table doesn't exist yet
    con.commit()


def connect():
    con = sqlite3.connect(str(DB_PATH))
    con.executescript(SCHEMA)
    _ensure_cols(con)
    return con


_LOCAL = threading.local()   # per-thread cached connection for the HOT per-tick path (SQLite conns aren't
                             # safe to share across threads, so one connection per calling thread)


def _tick_con():
    """A cached connection for record_tick — avoids a fresh connect()+executescript(SCHEMA)+close() on
    EVERY tick (dozens/lap). Schema is applied once when the thread's connection is first created."""
    con = getattr(_LOCAL, "con", None)
    if con is None:
        con = sqlite3.connect(str(DB_PATH))
        con.executescript(SCHEMA)
        _LOCAL.con = con
    return con


def _upsert(con, table, row, keys=("signal_id",)):
    row = {k: v for k, v in row.items() if v is not None or k in keys}
    if any(row.get(k) is None for k in keys):
        return
    cols = list(row)
    ph = ",".join("?" for _ in cols)
    setc = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    conflict = ",".join(keys)
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
           f"ON CONFLICT({conflict}) DO UPDATE SET {setc}") if setc else \
          f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})"
    con.execute(sql, [row[c] for c in cols])


def _ingest_events(con, r):
    """Explode a closed-trade record's management arc into queryable event rows."""
    sid = r.get("journal_id") or r.get("id")
    if not sid:
        return
    for i, a in enumerate(r.get("user_actions") or []):
        _upsert(con, "events", {"signal_id": sid, "source": "you", "seq": i,
                                "ts_utc": a.get("ts_utc") or a.get("ts"), "kind": a.get("kind"),
                                "qty": a.get("qty"), "price": _f(a.get("price")),
                                "realized_delta": _f(a.get("realized_delta")),
                                "stop_ref": str(a.get("stop")) if a.get("stop") is not None else None},
                keys=("signal_id", "source", "seq"))
    for i, a in enumerate(r.get("provider_events") or []):
        _upsert(con, "events", {"signal_id": sid, "source": "provider", "seq": i,
                                "ts_utc": a.get("ts_utc") or a.get("ts"), "kind": a.get("type"),
                                "price": _f(a.get("price")), "pct_out": _f(a.get("pct_out")),
                                "pct_up": _f(a.get("pct_up")), "stop_ref": a.get("stop_ref"),
                                "raw": (a.get("raw") or "")[:200]},
                keys=("signal_id", "source", "seq"))


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


def record_tick(row):
    """One observation in a trade's life — full conditions (option greeks + underlying + tape + momentum).
    Cheap, idempotent (keyed signal_id/ts_utc). Called from the monitor while shadowing / holding.
    Reuses a thread-local connection (no per-tick connect/executescript/close)."""
    try:
        con = _tick_con()
        _upsert(con, "path", row, keys=("signal_id", "ts_utc"))
        con.commit()
    except Exception:
        try: _LOCAL.con.close()           # a broken cached connection -> drop it so the next call reconnects
        except Exception: pass
        _LOCAL.con = None


def _shadow_scaleout(ticks, t1, t2, stop, t3=None):
    """Replay ordered option-%-move ticks through the desk's SCALE-OUT discipline — the exits the desk
    actually tells you to take, so the shadow measures YOUR strategy, not a naive all-out:
      · sell 1/2 at +t1, move the stop to BREAKEVEN
      · sell 1/4 at +t2, trail the stop up to +t1
      · the runner (1/4) books at +t3 if it gets there, else exits at the last observed tick / EOD
      · a hard stop at -stop before any fill = full -1R
    Returns the portion-weighted blended return %. Fixes both failure modes of the old binary model
    (spike-then-die was OVERstated as a full T1 win; a big runner was UNDERstated at just T1).
    t3 is optional: without it the runner never takes profit, which silently made every provider's t3
    decorative — chip emits a t3 per ticker and it was being discarded here."""
    if not ticks:
        return None
    rem, realized, slvl, hit1, hit2 = 1.0, 0.0, -abs(stop), False, False
    for pct in ticks:
        if rem <= 0:
            break
        if not hit1 and pct >= t1:
            realized += 0.50 * t1; rem -= 0.50; hit1 = True; slvl = 0.0        # 1/2 out, stop -> breakeven
        if hit1 and not hit2 and pct >= t2:
            realized += 0.25 * t2; rem -= 0.25; hit2 = True; slvl = t1         # 1/4 out, trail -> +t1
        if hit2 and t3 and pct >= t3:
            realized += rem * t3; rem = 0.0; break                            # runner books the final target
        if pct <= slvl:
            # Book at the OBSERVED tick, not at the level. Booking at `slvl` assumed a fill exactly at the
            # stop, discarding the gap between the last good tick and the one that breached it. Median
            # inter-tick gap is 63s (p90 88s) and an option routinely traverses several percent in that
            # time, so the old form gifted +0.65R per stopped shadow (+0.52R on the population mean) --
            # and DIFFERENTIALLY: sulker +1.37R, mike +1.07R, chip +0.09R. It was ranking providers by how
            # coarsely their paths happened to be sampled. min() keeps it a stop, never a windfall.
            realized += rem * min(pct, slvl); rem = 0.0; break
    if rem > 0:
        realized += rem * ticks[-1]                                           # runner exits at last observed tick
    return round(realized, 2)


# ---------------- EXECUTION COST: what the shadow actually costs to trade ----------------
# The shadow existed to measure provider edge, and that edge drives size_mult -> real money on
# tastytrade. It was measured MID-in / MID-out with no commission: an entry nobody can get, an exit
# nobody will pay you, and a broker who works for free. chip_desk._emit meanwhile priced ITS entry at
# the ASK, so chip and the human providers were not even on the same measuring stick.
#
# CONVENTION (v1) — you are a retail buyer of debit options with no size and no edge in the queue:
#   ENTRY  = the ASK. You are lifting the offer. `ticket.limit` (mid + 0.3x half-spread) is what you
#            *hope* to pay; the ask is what you *do* pay when the tape is moving, which is exactly when
#            these signals fire. Assuming a mid fill is assuming you are the liquidity provider.
#   EXIT   = the BID. Same argument, other direction.
#   COST   = a flat per-contract round trip, charged once against the whole position (the scale-out
#            closes in three pieces but closes the same number of contracts it opened, and tastytrade
#            charges commission only to OPEN an equity option).
# A tick is an EXIT OPPORTUNITY only if it carries a two-sided quote. ~50% of stored shadow ticks do
# not: IBKR returns -1 for bid/ask outside RTH and market._read_ticker passes that through while
# falling back to `last` for the mid. Booking a stop against an overnight `last` print is booking a
# fill nobody could have gotten. Those ticks stay in `path` (nothing is deleted) — they are simply not
# treated as places you could have transacted.
OPEN_COMM_PER_CT  = 1.00   # tastytrade: $1.00/contract to OPEN an equity option
OPEN_FEES_PER_CT  = 0.15   # exchange/regulatory/clearing, per contract
CLOSE_COMM_PER_CT = 0.00   # tastytrade: closing an equity option is commission-free
CLOSE_FEES_PER_CT = 0.15   # ORF/OCC still apply on the way out
RT_COST_PER_CT = OPEN_COMM_PER_CT + OPEN_FEES_PER_CT + CLOSE_COMM_PER_CT + CLOSE_FEES_PER_CT   # $1.30

SHADOW_BASIS = "askbid_comm_v1"      # bump this to force a one-time re-score of every shadow
_DEFAULT_HALF_SPREAD = 0.05          # last-resort modelled half-spread when no quote was ever observed
_REF_BAND = (0.25, 4.0)              # a quote this far from `ref` is a different CONTRACT, not a spread and
                                     # not a chase. Same 4x threshold server.py already uses to raise
                                     # price_mismatch -- one definition of "misparsed", used in both places.


def _tradeable(bid, ask):
    """A two-sided quote you could actually transact against. IBKR's 'no data' sentinel is -1, NOT NULL,
    so `bid is not None` is not enough — that was the trap."""
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


def _ref_scale(ref, card_mid):
    """Factor that maps PATH prices onto `ref`'s price scale.

    Normally 1.0. It is not 1.0 when the shadow's reference price and the contract actually being
    quoted are an order of magnitude apart — a misparse (wrong strike/expiry, or a number that was
    never a premium), which the desk flags separately as price_mismatch. Such a shadow is already
    meaningless; the job here is to make sure the cost fix leaves it exactly as meaningless as it was
    rather than silently re-basing it onto a different contract and inventing a new answer.

    Derived from immutable inputs only (never from a stored fill), so it is identical on every replay —
    which is what makes the re-score idempotent."""
    ref, cm = _f(ref), _f(card_mid)
    if not ref or not cm or ref <= 0 or cm <= 0:
        return 1.0
    lo, hi = _REF_BAND
    return 1.0 if lo <= cm / ref <= hi else ref / cm


def _shadow_entry_px(ref, card_bid, card_ask, card_mid, first_tick):
    """The price the trader would actually PAY, and where it came from. Degrades in documented steps:
      1. card_ask            — the ask from the same quote snapshot that produced `ref`. Exact.
      2. first_tradeable_ask — the first two-sided ask on the path. Exact, slightly later.
      3. rel_spread_on_ref   — (1) and (2) are out of band vs `ref`, i.e. the card quote and the shadow
                               reference describe DIFFERENT contracts (a misparse; the desk flags these
                               separately as price_mismatch). Substituting the absolute ask would
                               silently re-base the signal onto another contract, so instead we apply
                               the OBSERVED RELATIVE half-spread to `ref` and keep the scales aligned.
      4. modelled            — no quote was ever observed. Gross `ref` up by _DEFAULT_HALF_SPREAD.
    Never returns the mid: reverting to mid is the bug being fixed."""
    ref = abs(_f(ref) or 0) or 0.01
    lo, hi = _REF_BAND
    if _f(card_ask) and card_ask > 0 and lo <= card_ask / ref <= hi:
        return round(card_ask, 4), "card_ask"
    if first_tick and _f(first_tick.get("ask")) and first_tick["ask"] > 0 \
            and lo <= first_tick["ask"] / ref <= hi:
        return round(first_tick["ask"], 4), "first_tradeable_ask"
    if _f(card_ask) and _f(card_mid) and card_ask > 0 and card_mid > 0:
        return round(ref * (1 + (card_ask - card_mid) / card_mid), 4), "rel_spread_on_ref"
    return round(ref * (1 + _DEFAULT_HALF_SPREAD), 4), "modelled_half_spread"


def _shadow_r(con, signal_id, stop, t1, t2, t3=None):
    """Would-be blended return% + R for a shadowed signal, on BOTH bases.

    Returns (ret_honest, r_honest, ret_mid, r_mid, meta) where the honest pair is ASK-in / BID-out with
    the round-trip commission charged, and the mid pair is the legacy MID-in / MID-out score kept for
    audit. Both come from the same stored path, so the delta is purely the cost model."""
    rows = con.execute("SELECT opt_pct, opt_mid, bid, ask FROM path WHERE signal_id=? AND phase='shadow' "
                       "ORDER BY ts_utc", (signal_id,)).fetchall()
    if not rows or not stop:
        return None, None, None, None, {}

    # --- legacy MID basis (unchanged arithmetic, kept for the audit columns) ---
    ret_mid = _shadow_scaleout([r[0] for r in rows if r[0] is not None], t1, t2, stop, t3)
    r_mid = round(ret_mid / abs(stop), 3) if ret_mid is not None else None

    sg = con.execute("SELECT bid, ask, mid, limit_px, premium FROM signals WHERE signal_id=?",
                     (signal_id,)).fetchone()
    o = con.execute("SELECT entry_px, entry_fill_px, score_basis FROM outcomes WHERE signal_id=?",
                    (signal_id,)).fetchone()
    ref = (o[0] if o else None) or (sg[3] if sg else None) or (sg[4] if sg else None) or (sg[2] if sg else None)
    if not ref:
        return None, None, ret_mid, r_mid, {}

    trade = [{"mid": r[1], "bid": r[2], "ask": r[3]} for r in rows if _tradeable(r[2], r[3])]
    # A fill already charged on the CURRENT basis is reused verbatim -> re-running cannot drift, and a
    # live shadow's entry can't wander as later ticks arrive. Also honours the fill the CARD stamped at
    # ingest (basis still NULL, first score). A basis bump re-derives from scratch.
    prior = str(o[2]) if (o and o[2]) else ""
    fill = (o[1] if (o and o[1] and (not prior or prior.startswith(SHADOW_BASIS))) else None)
    if fill:
        # Reusing the value must not lose HOW it was obtained — inherit the entry marker the first
        # scoring stamped, or the audit trail quietly upgrades a modelled entry to a real one.
        src = "modelled_half_spread" if "modelled_entry" in prior else "stored_fill"
    else:
        fill, src = _shadow_entry_px(ref, sg[0] if sg else None, sg[1] if sg else None,
                                     sg[2] if sg else None, trade[0] if trade else None)
    fill = max(fill, 0.01)
    # Path prices live on the CARD's scale; the entry lives on `ref`'s. They are the same scale unless
    # the signal was a misparse, in which case both are rescaled together. Computed from ref+card_mid
    # ONLY -- deriving it from `src` made a reused fill silently drop the rescale on the second pass.
    k = _ref_scale(ref, sg[2] if sg else None)

    if trade:
        ticks, basis = [round((t["bid"] * k / fill - 1) * 100, 1) for t in trade], SHADOW_BASIS
    else:
        # DEGRADED: this shadow never once carried a two-sided quote (thin contract, or every tick landed
        # outside RTH). Rather than silently marking to mid, synthesise the bid as mid minus a modelled
        # half-spread and STAMP the outcome so the row is identifiable and discountable.
        ticks = [round((r[1] * (1 - _DEFAULT_HALF_SPREAD) * k / fill - 1) * 100, 1)
                 for r in rows if r[1] is not None]
        basis = SHADOW_BASIS + "|degraded_no_two_sided_quote"
    ret = _shadow_scaleout(ticks, t1, t2, stop, t3)
    if ret is None:
        return None, None, ret_mid, r_mid, {}
    cost_pct = RT_COST_PER_CT / (fill * 100.0) * 100.0        # $/contract as a % of premium paid
    ret = round(ret - cost_pct, 2)
    return (ret, round(ret / abs(stop), 3), ret_mid, r_mid,
            {"fill": round(fill, 4), "cost_pct": round(cost_pct, 3),
             "basis": basis if src != "modelled_half_spread" else basis + "|modelled_entry",
             "n_tradeable": len(trade)})


# RETAIN THE FULL TICK HISTORY. The original 30 days was reasoned from SCORING ("an ended shadow can't
# change again, so its path is spent") — but the path is not just an input to the score, it is the primary
# research asset: the minute-by-minute story of how every provider's call actually behaved, which is the
# only thing that can ever reveal a repeatable pattern. Deleting it optimises for a use we don't have
# (rescoring) at the cost of the one we do (finding edge).
# Cost is trivial: ~4.5k ticks/day at ~320 bytes = ~370 MB/year, against a DB currently at 17 MB.
# Caught 18 days before the first deletion would have fired — nothing has been lost.
# If size ever bites, DOWNSAMPLE old paths (keep entry/peak/trough/exit + every Nth tick), never delete.
_PATH_RETAIN_DAYS = None     # None == NEVER DELETE. Owner's standing instruction: nothing is deleted.
                             # Setting a number here re-enables destructive pruning — don't, without asking.
_SHADOW_ACTIVE_DAYS = 3      # a shadow whose path last grew within N days is still "live" -> worth re-scoring
_LAST_PRUNE = [0.0]          # once/hour gate (list so it's mutable from the nested call)


def prune_path(con):
    """DISABLED BY DEFAULT — retention is None, so this is a no-op and no tick is ever deleted.

    The `path` table is the minute-by-minute story of how every provider's call actually behaved. It is
    the primary research asset of this desk: the only thing that can reveal a repeatable pattern. The
    original 30-day window was reasoned from SCORING ("an ended shadow can't be rescored, so its path is
    spent"), which is true of the score and false of the research. It optimised for a use we don't have at
    the cost of the one we do.

    Kept as a function (not ripped out) so the call sites stay stable and so a future DOWNSAMPLER can live
    here — if size ever bites, thin old paths (keep entry/peak/trough/exit plus every Nth tick) rather than
    deleting them. Growth is ~370 MB/year, which buys a lot of years."""
    if not _PATH_RETAIN_DAYS:
        return
    now = time.time()
    if now - _LAST_PRUNE[0] < 3600:
        return
    _LAST_PRUNE[0] = now
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_PATH_RETAIN_DAYS)).isoformat()
    con.execute("DELETE FROM path WHERE ts_utc < ?", (cutoff,))


def _resync_shadow_r(con):
    """(Re)score shadow outcomes' return% + R from a scale-out replay of their path + the signal's own
    stop/target plan. Scoped to shadows still worth touching: never-scored (return_pct IS NULL) OR still
    live (path grew within _SHADOW_ACTIVE_DAYS). Long-ended shadows keep their stored, final R — replaying
    their full path every heavy lap was O(all-shadows × ticks) of pure waste."""
    from datetime import datetime, timedelta, timezone
    active_cutoff = (datetime.now(timezone.utc) - timedelta(days=_SHADOW_ACTIVE_DAYS)).isoformat()
    rows = con.execute(
        "SELECT o.signal_id, s.stop_pct, s.t1_pct, s.t2_pct, s.t3_pct FROM outcomes o "
        "JOIN signals s ON s.signal_id=o.signal_id WHERE o.kind='SHADOW' AND ("
        "  o.return_pct IS NULL"
        # A shadow scored on a SUPERSEDED cost basis is re-scored once, then never again — this is what
        # migrates the whole back-history when SHADOW_BASIS is bumped, without reopening the O(all x ticks)
        # replay the active-only scoping was added to close. `LIKE basis||'%'` keeps the degraded/modelled
        # suffixes (which are still the current basis) from re-qualifying every lap.
        "  OR o.score_basis IS NULL OR o.score_basis NOT LIKE ?"
        "  OR EXISTS ("
        "    SELECT 1 FROM path p WHERE p.signal_id=o.signal_id AND p.phase='shadow' AND p.ts_utc > ?))",
        (SHADOW_BASIS + "%", active_cutoff)).fetchall()
    for sid, sp, t1, t2, t3 in rows:
        sp = sp or 30; t1 = t1 or 40; t2 = t2 or (t1 * 2)
        ret, r, ret_mid, r_mid, meta = _shadow_r(con, sid, sp, t1, t2, t3)  # t3 None -> runner rides to last tick
        if r is None:
            continue
        con.execute(
            "UPDATE outcomes SET return_pct=?, r_multiple=?, return_pct_mid=?, r_mid=?, "
            "entry_fill_px=?, cost_pct=?, score_basis=?, n_tick_tradeable=? "
            "WHERE signal_id=? AND kind='SHADOW'",
            (ret, r, ret_mid, r_mid, meta.get("fill"), meta.get("cost_pct"),
             meta.get("basis") or SHADOW_BASIS, meta.get("n_tradeable"), sid))


def _shadow_outcome(e):
    """Would-be outcome of a shadowed signal: disciplined WIN/LOSS/SCRATCH label (target vs stop, first hit)
    PLUS peak/trough over its life. return%/R are (re)computed by _resync_shadow_r via a scale-out replay."""
    sh = e.get("shadow") or {}
    res = sh.get("result") or (sh.get("state") if sh.get("state") in ("WIN", "LOSS") else None)
    if res not in ("WIN", "LOSS", "SCRATCH"):
        return None
    return {"signal_id": e.get("id"), "kind": "SHADOW", "result": res,
            "realized_usd": None, "r_multiple": None, "return_pct": _f(sh.get("result_pct")),
            "entry_px": sh.get("ref"), "entry_fill_px": _f(sh.get("fill")), "exit_px": None,
            "peak_pct": _f(sh.get("peak")), "trough_pct": _f(sh.get("trough")),
            "hold_secs": None, "opened_utc": sh.get("start_utc"), "closed_utc": sh.get("ended_utc")}


def _entry_from_closed(r):
    """Reconstruct a signal-feature row from a closed trade's own origin — so an outcome/event whose
    original card was never archived still has a parent signal (no orphans)."""
    o = r.get("origin") or {}
    sig = o.get("sig") or {"ticker": r.get("ticker"), "strike": r.get("strike"), "type": r.get("type"),
                           "premium": r.get("avg_entry"), "expiry": r.get("expiry")}
    return {"id": r.get("journal_id") or r.get("id"), "ts_utc": o.get("ts_utc") or r.get("opened_utc"),
            "provider": r.get("provider"), "sig": sig, "score": o.get("score"), "tier_key": o.get("tier"),
            "conviction": o.get("conviction"), "size_mult": o.get("size_mult"), "plan": o.get("plan"),
            "opt": r.get("entry_greeks"), "verified": o.get("verified"), "raw": o.get("raw"), "cosign": o.get("cosign")}


def sync_outcomes(signals=None):
    """Upsert REALIZED outcomes from closed_trades.jsonl + SHADOW outcomes from live signals.
    Cheap + idempotent — safe to call every monitor cycle."""
    try:
        con = connect()
        for f in glob.glob(str(_P.CHANNELS / "*" / "closed_trades.jsonl")):
            for ln in Path(f).read_text().splitlines():
                if not ln.strip():
                    continue
                r = json.loads(ln)
                sid = r.get("journal_id") or r.get("id")
                if not sid:
                    continue
                if not con.execute("SELECT 1 FROM signals WHERE signal_id=?", (sid,)).fetchone():
                    _upsert(con, "signals", _signal_row(_entry_from_closed(r)))   # stub the parent signal
                rz = r.get("realized")
                res = "WIN" if (rz or 0) > 0 else ("LOSS" if (rz or 0) < 0 else "SCRATCH")
                _upsert(con, "outcomes", {
                    "signal_id": sid, "kind": "REALIZED", "result": res,
                    "realized_usd": _f(rz), "r_multiple": _f(r.get("R_multiple")),
                    "return_pct": _f(r.get("return_pct")), "exit_vs_peak_pct": _f(r.get("exit_vs_peak_pct")),
                    "prov_pct_out": _f(r.get("provider_pct_out_final")), "your_pct_out": _f(r.get("your_pct_out_final")),
                    "entry_px": _f(r.get("avg_entry")), "exit_px": _f(r.get("avg_exit")),
                    "peak_pct": _f(r.get("peak_pct")), "hold_secs": r.get("hold_secs"),
                    "opened_utc": r.get("opened_utc"), "closed_utc": r.get("closed_utc")})
                _ingest_events(con, r)                # every trim / cut / provider exit as its own row
        for e in (signals or []):
            row = _shadow_outcome(e)
            if row:
                # don't overwrite a REALIZED outcome with a shadow (taken beats shadow)
                cur = con.execute("SELECT kind FROM outcomes WHERE signal_id=?", (row["signal_id"],)).fetchone()
                if not cur or cur[0] == "SHADOW":
                    _upsert(con, "outcomes", row)
        _resync_shadow_r(con)                 # score shadow return%/R from the scale-out replay (live + backfill)
        prune_path(con)                       # rolling retention on the unbounded path table (once/hour)
        con.commit(); con.close()
    except Exception:
        pass


# ---------------- backfill / export / summary ----------------
def backfill():
    con = connect()
    seen = 0
    # 1) current cards
    fp = _P.CHANNELS / "_signals.json"
    entries = json.loads(fp.read_text()) if fp.exists() else []
    # 2) archived signals (full card history)
    for f in glob.glob(str(_P.CHANNELS / "*" / "live_signals.jsonl")):
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


SCORE_BUCKETS = [(0, 45, "SKIP <45"), (45, 60, "LOW 45-59"), (60, 75, "MED 60-74"), (75, 101, "HIGH 75+")]


def _pearson(pairs):
    n = len(pairs)
    if n < 3:
        return None
    xs = [a for a, b in pairs]
    ys = [b for a, b in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return round(num / den, 3) if den else None


def _calib_stats(sub):
    rs = [x["r"] for x in sub if x["r"] is not None]
    us = [x["usd"] for x in sub if x["usd"] is not None]
    n = len(sub)
    wins = sum(1 for x in sub if (x["r"] if x["r"] is not None else x["usd"] or 0) > 0)
    return {"n": n,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "win_pct": round(wins / n * 100) if n else None,
            "avg_usd": round(sum(us) / len(us)) if us else None,
            "total_usd": round(sum(us)) if us else None}


def _calib_verdict(n, pr_r):
    """Honest read: is the score EARNING trust? Significance ≈ |r| > 2/sqrt(n)."""
    if n < 15:
        return f"too few closed trades (n={n}) — keep logging; need ~30+ before the score can be trusted"
    sig = (2 / (n ** 0.5)) if n else 1
    if pr_r is None:
        return "insufficient data"
    if pr_r >= sig and pr_r >= 0.3:
        return f"score is TRACKING outcomes (r={pr_r}, significant at n={n}) — higher score = better R"
    if pr_r >= 0.15:
        return f"weak positive (r={pr_r}) — directionally right but NOT yet significant (need r>{sig:.2f} at n={n})"
    if pr_r < 0:
        return f"score is INVERTED vs outcomes (r={pr_r}) — re-weight needed"
    return f"score not separating outcomes yet (r={pr_r}) — needs re-weighting or more data"


def calibration():
    """Is the score/grade actually tracking realized outcomes? Per-bucket realized R + win% + $ on the
    closed trades we have. This is the loop that keeps the score honest — watch it as n grows; the score
    earns trust only when the buckets separate AND the correlation clears significance."""
    con = connect()
    rows = con.execute("SELECT score, grade, r_multiple, realized_usd, result FROM ml_dataset "
                       "WHERE r_multiple IS NOT NULL OR realized_usd IS NOT NULL").fetchall()
    con.close()
    closed = [{"score": r[0], "grade": r[1], "r": r[2], "usd": r[3], "result": r[4]} for r in rows]
    score_rows = [{"bucket": lbl, "lo": lo, "hi": hi,
                   **_calib_stats([x for x in closed if x["score"] is not None and lo <= x["score"] < hi])}
                  for lo, hi, lbl in SCORE_BUCKETS]
    grade_rows = [{"grade": g, **_calib_stats([x for x in closed if x["grade"] == g])} for g in ("A", "B", "C", "D")]
    pr_r = _pearson([(x["score"], x["r"]) for x in closed if x["score"] is not None and x["r"] is not None])
    pr_usd = _pearson([(x["score"], x["usd"]) for x in closed if x["score"] is not None and x["usd"] is not None])
    seq = [b["avg_r"] for b in score_rows if b["avg_r"] is not None]
    monotonic = all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)) if len(seq) >= 2 else None
    return {"n_closed": len(closed), "score_buckets": score_rows, "grade_buckets": grade_rows,
            "pearson_r": pr_r, "pearson_usd": pr_usd, "monotonic": monotonic,
            "verdict": _calib_verdict(len(closed), pr_r)}


def _exit_verdict(n, avg_loss_r, n_whip, n_loss):
    if n < 12:
        return f"too few closed trades (n={n}) — keep logging to read the exit leak"
    issues = []
    if avg_loss_r is not None and avg_loss_r < -1.1:
        issues.append(f"losses OVERRUN the stop (avg {avg_loss_r}R vs -1.0 target) — cut faster")
    if n_whip and n_loss and n_whip / n_loss >= 0.2:
        issues.append(f"{n_whip} whipsaws — winners round-tripping to losses; bank the pop sooner")
    return " · ".join(issues) if issues else "exits look disciplined — stops honored, few round-trips"


def exit_quality():
    """How well are the EXITS executed? Direction is a coin flip, so THIS is where the edge lives.
    Measures the two proven leaks on YOUR real closed trades (kind=REALIZED, not shadows): losses that
    overrun the -1R stop, and winners that round-trip (peaked then closed red)."""
    con = connect()
    rows = con.execute("SELECT peak_pct, r_multiple, exit_vs_peak_pct, result FROM outcomes "
                       "WHERE kind='REALIZED' AND r_multiple IS NOT NULL").fetchall()
    con.close()
    o = [{"peak": r[0], "r": r[1], "evp": r[2], "result": r[3]} for r in rows]
    n = len(o)
    if not n:
        return {"n": 0}
    wins = [x for x in o if x["r"] > 0]
    loss = [x for x in o if x["r"] < 0]
    avg_loss_r = round(sum(x["r"] for x in loss) / len(loss), 2) if loss else None
    overrun_pct = round((abs(avg_loss_r) - 1) * 100) if avg_loss_r is not None else None   # % beyond 1R
    whip = [x for x in o if (x["peak"] or 0) >= 25 and x["r"] < 0]                          # peaked then lost
    gb = [x["evp"] for x in wins if x["evp"] is not None]                                   # exit vs peak (neg=gave back)
    return {"n": n, "win_pct": round(len(wins) / n * 100),
            "avg_loss_r": avg_loss_r, "overrun_pct": overrun_pct,
            "whipsaws": len(whip), "whip_of_loss": round(len(whip) / len(loss) * 100) if loss else 0,
            "avg_giveback": round(sum(gb) / len(gb), 1) if gb else None,
            "avg_peak": round(sum((x["peak"] or 0) for x in o) / n, 1),
            "verdict": _exit_verdict(n, avg_loss_r, len(whip), len(loss))}


SHADOW_WEIGHT = 0.35     # a shadow observation counts ~1/3 of a realized one (lower confidence)
SHADOW_HAIRCUT = 0.10    # subtract 0.10R from shadow avg — shadow assumes ideal mechanical exits; reality slips


def provider_edge():
    """Per-provider BLENDED-R edge + a DATA-GATED size multiplier.

    Blends TWO tracks so size moves on real evidence without waiting forever for a realized sample:
      · REALIZED R (your actual fills) — unbiased, full weight, but low-n.
      · SHADOW R (would-be, scale-out replay) — high-n, but assumes ideal execution, so it's HAIRCUT
        (−0.10R for slippage) and DOWN-WEIGHTED (≈1/3 of a realized obs).
    SHRINKING a bleeder can trigger on the blend alone (safe direction). TILTING UP additionally requires
    ≥5 real fills — never size real money up on pure hypothesis. Capped so no provider oversizes the acct."""
    con = connect()
    rows = con.execute("SELECT s.provider, o.kind, o.r_multiple FROM outcomes o JOIN signals s ON s.signal_id=o.signal_id "
                       "WHERE o.r_multiple IS NOT NULL AND o.kind IN ('REALIZED','SHADOW')").fetchall()
    con.close()
    from collections import defaultdict
    realized, shadow = defaultdict(list), defaultdict(list)
    for prov, kind, r in rows:
        (realized if kind == "REALIZED" else shadow)[prov].append(r)
    out = {}
    for prov in set(realized) | set(shadow):
        rr, ss = realized.get(prov, []), shadow.get(prov, [])
        n_r, n_s = len(rr), len(ss)
        avg_r = (sum(rr) / n_r) if n_r else None
        avg_s = (sum(ss) / n_s) if n_s else None
        w_r, w_s = float(n_r), n_s * SHADOW_WEIGHT                      # shadow down-weighted
        num = (avg_r * w_r if n_r else 0.0) + ((avg_s - SHADOW_HAIRCUT) * w_s if n_s else 0.0)  # shadow haircut
        den = w_r + w_s
        blended = (num / den) if den else None
        eff_n = n_r + w_s                                              # effective sample size
        if blended is None or eff_n < 8:
            mult = 1.0                                                 # not enough data to move either way
        elif blended <= -0.30:
            mult = 0.25                                                # bleeder — shrink (shadow may trigger; safe)
        elif blended <= -0.10:
            mult = 0.50
        elif blended >= 0.30 and eff_n >= 20 and n_r >= 5:
            mult = 1.50                                                # tilt-UP needs REAL fills, not hypothesis
        elif blended >= 0.10 and eff_n >= 20 and n_r >= 5:
            mult = 1.25
        else:
            mult = 1.0
        out[prov] = {"mult": mult,
                     "n": n_r, "avg_r": round(avg_r, 2) if avg_r is not None else None,  # back-compat keys
                     "realized_n": n_r, "realized_r": round(avg_r, 2) if avg_r is not None else None,
                     "shadow_n": n_s, "shadow_r": round(avg_s, 2) if avg_s is not None else None,
                     "blended_r": round(blended, 2) if blended is not None else None,
                     "eff_n": round(eff_n, 1)}
    return out


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
    n_ev = q("SELECT COUNT(*) FROM events")
    you = q("SELECT COUNT(*) FROM events WHERE source=?", "you")
    prov = q("SELECT COUNT(*) FROM events WHERE source=?", "provider")
    print(f"  events   : {n_ev}  (your trims/cuts {you}, provider exits {prov})")
    print(f"  path ticks: {q('SELECT COUNT(*) FROM path')}  (peaks {q('SELECT COUNT(*) FROM path WHERE is_peak=1')})")
    print(f"  ml rows w/ label: {labeled}")
    print("\n  by provider (signals / labeled):")
    for prov, n, lab in con.execute(
            "SELECT s.provider, COUNT(*), COUNT(m.label_win) FROM signals s "
            "LEFT JOIN ml_dataset m ON m.signal_id=s.signal_id GROUP BY s.provider ORDER BY 2 DESC"):
        print(f"    {str(prov):<13} {n:>4} / {lab or 0}")
    con.close()


def validate():
    """Chip validates the backend chips — integrity, completeness, data quality, source consistency."""
    con = connect()
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    checks = []

    def C(ok, name, detail="", warn=False):
        checks.append(("warn" if (not ok and warn) else ("pass" if ok else "fail"), name, detail))

    n_sig = q("SELECT COUNT(*) FROM signals")
    # 1) referential integrity — no orphan rows
    for tbl in ("decisions", "outcomes", "events", "path"):
        orph = q(f"SELECT COUNT(*) FROM {tbl} t "
                 f"LEFT JOIN signals s ON s.signal_id=t.signal_id WHERE s.signal_id IS NULL")
        C(orph == 0, f"{tbl}: every row links to a real signal", f"{orph} orphan(s)")
    # 2) completeness
    taken = q("SELECT COUNT(*) FROM decisions WHERE action='TAKEN'")
    skipped = q("SELECT COUNT(*) FROM decisions WHERE action='SKIPPED'")
    taken_no_out = q("SELECT COUNT(*) FROM decisions d WHERE action='TAKEN' "
                     "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.signal_id=d.signal_id)")
    C(taken_no_out == 0, "every TAKEN trade has an outcome", f"{taken_no_out} open/unrecorded", warn=True)
    shadow_res = q("SELECT COUNT(*) FROM outcomes WHERE kind='SHADOW'")
    C(True, "skips resolving (shadow)", f"{shadow_res}/{skipped} resolved, rest tracking")
    # 3) data quality — sane ranges
    C(q("SELECT COUNT(*) FROM signals WHERE premium IS NOT NULL AND premium<=0") == 0, "premiums positive")
    C(q("SELECT COUNT(*) FROM signals WHERE iv IS NOT NULL AND (iv<0 OR iv>5)") == 0, "IV in [0,5]")
    C(q("SELECT COUNT(*) FROM signals WHERE delta IS NOT NULL AND ABS(delta)>1.01") == 0, "delta in [-1,1]")
    C(q("SELECT COUNT(*) FROM signals WHERE grade IS NOT NULL AND grade NOT IN ('A','B','C','D')") == 0, "grade in A-D")
    C(q("SELECT COUNT(*) FROM (SELECT signal_id FROM signals GROUP BY signal_id HAVING COUNT(*)>1)") == 0,
      "no duplicate signals")
    C(q("SELECT COUNT(*) FROM outcomes WHERE result NOT IN ('WIN','LOSS','SCRATCH')") == 0, "outcome results valid")
    # 4) source consistency — every jsonl signal made it into the DB
    src = set()
    fp = _P.CHANNELS / "_signals.json"
    if fp.exists():
        src |= {e.get("id") for e in json.loads(fp.read_text()) if e.get("id")}
    for f in glob.glob(str(_P.CHANNELS / "*" / "live_signals.jsonl")):
        for ln in Path(f).read_text().splitlines():
            if ln.strip():
                i = json.loads(ln).get("id")
                if i:
                    src.add(i)
    db_ids = {r[0] for r in con.execute("SELECT signal_id FROM signals")}
    missing = src - db_ids
    C(len(missing) == 0, "all source signals captured in DB", f"{len(missing)} missing — run backfill", warn=True)
    with_dec = q("SELECT COUNT(DISTINCT signal_id) FROM decisions")
    with_path = q("SELECT COUNT(DISTINCT signal_id) FROM path")

    print(f"\n  {'='*54}\n  CHIP VALIDATION — backend data integrity\n  {'='*54}")
    for st, name, detail in checks:
        icon = {"pass": "  ✅", "warn": "  ⚠️", "fail": "  ❌"}[st]
        print(icon + f" {name}" + (f" — {detail}" if detail else ""))
    print(f"\n  coverage: {n_sig} signals · {with_dec} decided · {taken} taken · {skipped} skipped · {with_path} with path ticks")
    ok = all(st != "fail" for st, _, _ in checks)
    print(f"  {'✅ ALL CHECKS PASS — backend is sound' if ok else '❌ ISSUES FOUND (see above)'}\n")
    con.close()
    return ok


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "validate":
        import sys as _s
        _s.exit(0 if validate() else 1)
    if cmd in ("calibrate", "calibration", "calib"):
        c = calibration()
        print(f"\n  SCORE CALIBRATION  ({c['n_closed']} closed trades)\n  {'-'*54}")
        print(f"  {'bucket':<11}{'n':>4}{'avg R':>8}{'win%':>7}{'avg $':>8}{'total $':>9}")
        for b in c["score_buckets"]:
            r = f"{b['avg_r']:+.2f}" if b['avg_r'] is not None else "—"
            w = f"{b['win_pct']}%" if b['win_pct'] is not None else "—"
            au = f"{b['avg_usd']:+d}" if b['avg_usd'] is not None else "—"
            tu = f"{b['total_usd']:+d}" if b['total_usd'] is not None else "—"
            print(f"  {b['bucket']:<11}{b['n']:>4}{r:>8}{w:>7}{au:>8}{tu:>9}")
        print(f"\n  grade:  " + "   ".join(f"{g['grade']}={g['avg_r']:+.2f}(n{g['n']})" for g in c["grade_buckets"] if g['avg_r'] is not None))
        print(f"  Pearson r(score, R) = {c['pearson_r']}   monotonic={c['monotonic']}")
        print(f"\n  VERDICT: {c['verdict']}\n")
    elif cmd in ("exits", "exit", "exitquality"):
        e = exit_quality()
        if not e.get("n"):
            print("  no closed trades yet"); raise SystemExit
        print(f"\n  EXIT QUALITY  ({e['n']} real closed trades)\n  {'-'*54}")
        print(f"  win rate       : {e['win_pct']}%")
        print(f"  avg loss       : {e['avg_loss_r']}R" + (f"  ({e['overrun_pct']:+d}% vs the -1R stop)" if e['overrun_pct'] is not None else ""))
        print(f"  whipsaws       : {e['whipsaws']}  ({e['whip_of_loss']}% of losses were round-tripped wins)")
        print(f"  avg peak run   : +{e['avg_peak']}%")
        print(f"  winner giveback: {e['avg_giveback']}% below peak" if e['avg_giveback'] is not None else "  winner giveback: —")
        print(f"\n  VERDICT: {e['verdict']}\n")
    elif cmd == "backfill":
        print(f"  backfilled {backfill()} signals -> {DB_PATH.name}")
        summary()
    elif cmd == "export":
        n, p = export_csv(sys.argv[2] if len(sys.argv) > 2 else "ml_dataset.csv")
        print(f"  exported {n} rows -> {p}")
    else:
        summary()
