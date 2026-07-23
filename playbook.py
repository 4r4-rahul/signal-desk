#!/usr/bin/env python3
"""
playbook.py — the desk's IMMUTABLE, ACID, tamper-evident system of record.

This is Phase 1 of the agenda: before any agentic decision layer is trusted, the SUBSTRATE it writes to
must be auditable. Every thesis, decision, fill, management action, exit, and the REASON behind each is
appended here as one event in an ordered, hash-chained log. Nothing is ever updated or deleted — a
correction is a NEW event that references the one it supersedes, so the full history (including the
mistake) is always replayable. That is the whole point: a system whose decisions can be re-derived and
whose record cannot be quietly rewritten is worth more than a cleverer one that cannot be audited.

ACID, concretely:
  · Atomicity  — every append is a single BEGIN IMMEDIATE ... COMMIT; a crash mid-write rolls back whole.
  · Consistency— each row's hash chains to the previous row's hash; verify_chain() proves no gap or edit.
  · Isolation  — BEGIN IMMEDIATE takes the write lock up front, so the prev-hash read and the insert are
                 one serialized unit; concurrent writers queue, they never interleave a broken chain.
  · Durability — WAL journal + synchronous=FULL; a committed event survives a process kill.

Append-only is enforced in CODE (no UPDATE/DELETE path exists here) AND is consistent with the desk's
standing rule that nothing is ever deleted.
"""
import json, sqlite3, hashlib, threading, time
from datetime import datetime, timezone

import deskpaths as _P

PB_FILE = _P.DATA_DIR / "playbook.db"
GENESIS = "0" * 64            # prev_hash of the very first event

# The vocabulary of the record. Kept small and explicit; unknown kinds are rejected, not silently stored,
# so the log cannot accumulate typo'd event types that a later query would miss.
KINDS = {
    "THESIS",      # a reason to look at something: instrument, direction, horizon, why
    "DECISION",    # TAKE / SKIP / WATCH, and the rationale
    "ENTRY",       # a fill: price, qty, time
    "MANAGE",      # a trim / stop-move / add
    "EXIT",        # a close, with the reason (stop / target / time / thesis-break)
    "OUTCOME",     # the resolved result: R, return%, cost-honest
    "CORRECTION",  # supersedes a prior event (references its seq); the original stays in the log
    "NOTE",        # free-form audit note
}
ACTORS = {"regex", "llm", "chip", "human", "monitor", "system"}

_LOCAL = threading.local()   # one connection per thread (SQLite connections are not thread-safe to share)
_INIT = {"done": False}
_INIT_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _con():
    con = getattr(_LOCAL, "con", None)
    if con is None:
        con = sqlite3.connect(str(PB_FILE), timeout=10.0, isolation_level=None)  # autocommit off via explicit BEGIN
        con.execute("PRAGMA journal_mode=WAL")        # durable + lets readers run during a write
        con.execute("PRAGMA synchronous=FULL")        # a COMMITted event survives a crash
        con.execute("PRAGMA foreign_keys=ON")
        _LOCAL.con = con
    return con


def _ensure():
    if _INIT["done"]:
        return
    with _INIT_LOCK:
        if _INIT["done"]:
            return
        con = _con()
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc     TEXT NOT NULL,
            signal_id  TEXT,
            kind       TEXT NOT NULL,
            actor      TEXT NOT NULL,
            horizon    TEXT,
            supersedes INTEGER,
            payload    TEXT NOT NULL,
            prev_hash  TEXT NOT NULL,
            hash       TEXT NOT NULL UNIQUE
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_pb_sig  ON events(signal_id)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_pb_kind ON events(kind)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_pb_ts   ON events(ts_utc)")
        con.commit()
        _INIT["done"] = True


def _row_hash(seq, ts, signal_id, kind, actor, horizon, supersedes, payload, prev_hash):
    """Deterministic hash over EVERY field, chained to the previous row. Any edit to any stored field
    breaks verify_chain(). The seq is included so a row cannot be moved."""
    h = hashlib.sha256()
    for part in (seq, ts, signal_id, kind, actor, horizon, supersedes, payload, prev_hash):
        h.update(b"\x1f")                              # unit separator so field boundaries can't be spoofed
        h.update(str(part if part is not None else "").encode("utf-8"))
    return h.hexdigest()


def append(kind, *, signal_id=None, actor="system", horizon=None, supersedes=None, **payload):
    """Append ONE event. Atomic and serialized: BEGIN IMMEDIATE takes the write lock, so reading the tip
    hash and inserting the new row cannot be interleaved by another writer. Returns the new seq, or None
    on a validated rejection (unknown kind/actor) — never raises into the caller's hot path."""
    if kind not in KINDS or actor not in ACTORS:
        return None
    _ensure()
    con = _con()
    try:
        # allow_nan=False so a non-finite value can never enter the record (and would break strict-finite
        # readers downstream). Serialize BEFORE opening the transaction, and fail closed to None rather than
        # raising into the caller's hot path — an un-appendable event must never take down an ingest lap.
        body = json.dumps(payload, default=str, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        return None
    ts = _now()
    for attempt in range(6):                           # WAL still serializes writers; retry on a busy lock
        try:
            con.execute("BEGIN IMMEDIATE")
            tip = con.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = tip[1] if tip else GENESIS
            next_seq = (tip[0] + 1) if tip else 1
            row_hash = _row_hash(next_seq, ts, signal_id, kind, actor, horizon, supersedes, body, prev_hash)
            con.execute(
                "INSERT INTO events (seq, ts_utc, signal_id, kind, actor, horizon, supersedes, payload, "
                "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (next_seq, ts, signal_id, kind, actor, horizon, supersedes, body, prev_hash, row_hash))
            con.execute("COMMIT")
            return next_seq
        except sqlite3.OperationalError:               # database is locked -> back off and retry
            try: con.execute("ROLLBACK")
            except Exception: pass
            time.sleep(0.02 * (attempt + 1))
        except Exception:
            try: con.execute("ROLLBACK")
            except Exception: pass
            return None
    return None


def verify_chain():
    """Walk the whole log and prove it has not been edited, reordered, or had a row removed. Returns
    {ok, n, broken_at, reason}. This is the property that makes the record trustworthy: a single altered
    byte anywhere breaks the chain from that row forward, and it is detectable."""
    _ensure()
    con = _con()
    rows = con.execute(
        "SELECT seq, ts_utc, signal_id, kind, actor, horizon, supersedes, payload, prev_hash, hash "
        "FROM events ORDER BY seq ASC").fetchall()
    prev = GENESIS
    expect_seq = 1
    for r in rows:
        seq, ts, sid, kind, actor, horizon, sup, payload, prev_hash, stored = r
        if seq != expect_seq:
            return {"ok": False, "n": len(rows), "broken_at": seq, "reason": f"seq gap: expected {expect_seq}"}
        if prev_hash != prev:
            return {"ok": False, "n": len(rows), "broken_at": seq, "reason": "prev_hash does not chain"}
        calc = _row_hash(seq, ts, sid, kind, actor, horizon, sup, payload, prev_hash)
        if calc != stored:
            return {"ok": False, "n": len(rows), "broken_at": seq, "reason": "row hash mismatch (edited)"}
        prev = stored
        expect_seq += 1
    return {"ok": True, "n": len(rows), "broken_at": None, "reason": None}


def replay(signal_id):
    """The full ordered life of one trade — every thesis, decision, fill, management action and exit, with
    reasons. This is the 'playbook' for a single position, reconstructed from the immutable log."""
    _ensure()
    con = _con()
    rows = con.execute(
        "SELECT seq, ts_utc, kind, actor, horizon, supersedes, payload FROM events "
        "WHERE signal_id=? ORDER BY seq ASC", (signal_id,)).fetchall()
    out = []
    superseded = {r[5] for r in rows if r[5] is not None}
    for seq, ts, kind, actor, horizon, sup, payload in rows:
        try: body = json.loads(payload)
        except Exception: body = {"_unparsed": payload}
        out.append({"seq": seq, "ts": ts, "kind": kind, "actor": actor, "horizon": horizon,
                    "supersedes": sup, "superseded": seq in superseded, "payload": body})
    return out


def tail(n=50, kind=None):
    """Most-recent n events (optionally of one kind) for the monitor/UI. Read-only, never mutates."""
    _ensure()
    con = _con()
    if kind:
        rows = con.execute("SELECT seq, ts_utc, signal_id, kind, actor, horizon, payload FROM events "
                           "WHERE kind=? ORDER BY seq DESC LIMIT ?", (kind, n)).fetchall()
    else:
        rows = con.execute("SELECT seq, ts_utc, signal_id, kind, actor, horizon, payload FROM events "
                           "ORDER BY seq DESC LIMIT ?", (n,)).fetchall()
    out = []
    for seq, ts, sid, kind, actor, horizon, payload in rows:
        try: body = json.loads(payload)
        except Exception: body = {"_unparsed": payload}
        out.append({"seq": seq, "ts": ts, "signal_id": sid, "kind": kind, "actor": actor,
                    "horizon": horizon, "payload": body})
    return out


def stats():
    _ensure()
    con = _con()
    n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    by = dict(con.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall())
    return {"n": n, "by_kind": by, "chain": verify_chain()["ok"]}


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2))
