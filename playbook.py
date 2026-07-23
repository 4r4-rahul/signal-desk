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
  · Consistency— each row's hash chains to the previous; verify_chain() detects in-table edits, gaps,
                 reordering, and (via an external anchor file) tail truncation. It is tamper-EVIDENT, not
                 tamper-proof: a local party who can rewrite both the DB and the anchor can forge forward.
                 That bound is stated honestly, not hidden — see verify_chain().
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
ANCHOR_FILE = _P.DATA_DIR / "playbook.anchor"   # external tip (seq,hash) — catches TABLE TRUNCATION, which
                                                # an in-table chain alone cannot, since a deleted tail has
                                                # no successor whose prev_hash would break.
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
_DROPPED = {"n": 0}          # events lost to sustained lock exhaustion — surfaced in stats() (LOW-1)


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


def _write_anchor(seq, tip_hash):
    """Persist the tip (seq, hash) OUTSIDE the events table. verify_chain() cross-checks against it, so
    deleting the last N rows — which the in-table chain alone cannot catch, having no successor to break —
    is detected as 'seq below the anchored tip'. Best-effort: a missing/writeable-failed anchor degrades
    to in-table-only verification, never blocks a write."""
    try:
        tmp = str(ANCHOR_FILE) + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps({"seq": seq, "hash": tip_hash}))
        import os as _os
        _os.replace(tmp, str(ANCHOR_FILE))             # atomic swap so the anchor is never half-written
    except Exception:
        pass


def _read_anchor():
    try:
        return json.loads(ANCHOR_FILE.read_text())
    except Exception:
        return None


def _row_hash(seq, ts, signal_id, kind, actor, horizon, supersedes, payload, prev_hash):
    """Deterministic hash over EVERY field, chained to the previous row. Any edit to any stored field
    breaks verify_chain(). The seq is included so a row cannot be moved."""
    h = hashlib.sha256()
    for part in (seq, ts, signal_id, kind, actor, horizon, supersedes, payload, prev_hash):
        h.update(b"\x1f")                              # unit separator so field boundaries can't be spoofed
        # None and "" must hash DIFFERENTLY, or a field could be flipped None<->"" with the hash untouched
        # (MED-1). A NUL-byte tag for None can't collide with any str() output, which never contains \x00.
        h.update(b"\x00" if part is None else str(part).encode("utf-8"))
    return h.hexdigest()


def append(kind, *, signal_id=None, actor="system", horizon=None, supersedes=None, **payload):
    """Append ONE event. Atomic and serialized: BEGIN IMMEDIATE takes the write lock, so reading the tip
    hash and inserting the new row cannot be interleaved by another writer. Returns the new seq, or None
    on a validated rejection (unknown kind/actor) — never raises into the caller's hot path."""
    if kind not in KINDS or actor not in ACTORS:
        return None
    # CANONICALIZE the scalar fields to EXACTLY their stored SQLite types, once, before both hashing and
    # inserting. Without this, append() hashes the Python value's str() while verify_chain() hashes the
    # value SQLite hands back after type-affinity coercion — e.g. supersedes=2.0 is hashed as "2.0" but
    # stored/reread as INTEGER 2 ("2"), so verify would falsely brand a clean row (and the whole ledger
    # after it) as tampered. The most dangerous failure a system-of-record can have: a false "corrupted"
    # alarm. Canonicalizing makes the round trip an identity, so the two hash paths are equal by
    # construction, not by luck. (HIGH-1, external audit 2026-07-22.)
    try:
        signal_id = None if signal_id is None else str(signal_id)
        horizon = None if horizon is None else str(horizon)
        supersedes = None if supersedes is None else int(supersedes)
        kind = str(kind); actor = str(actor)
    except (TypeError, ValueError):
        return None
    try:
        # _ensure()/_con() are inside the try too: an unwritable dir / corrupt db / full disk must fail
        # closed to None, not raise — the module's "never raises into the hot path" contract (LOW-2).
        _ensure()
        con = _con()
        # allow_nan=False so a non-finite value can never enter the record (and would break strict-finite
        # readers downstream). Serialize BEFORE the transaction; fail closed to None, never raise.
        body = json.dumps(payload, default=str, sort_keys=True, allow_nan=False)
    except Exception:
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
            _write_anchor(next_seq, row_hash)          # external tip -> truncation becomes detectable
            return next_seq
        except sqlite3.OperationalError:               # database is locked -> back off and retry
            try: con.execute("ROLLBACK")
            except Exception: pass
            time.sleep(0.02 * (attempt + 1))
        except Exception:
            try: con.execute("ROLLBACK")
            except Exception: pass
            return None
    _DROPPED["n"] += 1                                  # a sustained lock storm dropped an event -> OBSERVABLE
    return None


def verify_chain():
    """Walk the log and check integrity. Returns {ok, n, broken_at, reason}.

    HONEST SCOPE (corrected after an external audit, 2026-07-22 — the earlier docstring overclaimed):
    This is tamper-EVIDENT, not tamper-PROOF. It reliably detects:
      · an in-table edit that leaves a successor un-re-hashed (the chain breaks from that row forward),
      · a seq gap or reordering,
      · tail TRUNCATION — via the external anchor cross-check below, which an in-table chain alone cannot
        catch (a deleted last row has no successor whose prev_hash would break).
    What it CANNOT prevent or fully detect: a party with write access to BOTH the DB file AND the anchor
    file can edit any row and recompute prev_hash/hash forward for every successor plus rewrite the anchor,
    and this will pass. Preventing that needs an EXTERNAL sink (an append-only store the desk can't rewrite,
    or a periodic notarized checkpoint) and is out of scope for a single-host ledger. This detects
    accident, bug, and casual tampering; it does not defend against a determined local attacker."""
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
    # external anchor cross-check: catches truncation (rows deleted off the tip).
    anc = _read_anchor()
    if anc:
        if len(rows) < anc.get("seq", 0):
            return {"ok": False, "n": len(rows), "broken_at": anc["seq"],
                    "reason": f"truncated: anchor tip seq {anc['seq']} > table len {len(rows)}"}
        if rows and anc.get("seq") == rows[-1][0] and anc.get("hash") != rows[-1][9]:
            return {"ok": False, "n": len(rows), "broken_at": rows[-1][0], "reason": "tip hash != anchor"}
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
    return {"n": n, "by_kind": by, "chain": verify_chain()["ok"], "dropped": _DROPPED["n"]}


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2))
