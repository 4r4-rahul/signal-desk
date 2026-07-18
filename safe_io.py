# safe_io.py — the single funnel for every AUTHORITATIVE full-file write of desk state.
# Three guards: (1) anti-clobber tripwire (refuses a write that drastically shrinks a substantial file —
# this is what would have blocked the 141→1 incident), (2) rolling backup before overwrite, (3) atomic
# write (temp + os.replace). No external deps. A blocked write NEVER touches the live file.
import json, os, time, glob, hashlib
from pathlib import Path

_BAK_KEEP = 8                 # keep the last 8 backups per file (was 20)
_BAK_MIN_INTERVAL = 600       # at most one backup per file per 10 min — the atomic replace protects between
_BAK_LAST = {}                # path -> (last_backup_ts, last_content_hash) — skip unchanged/too-frequent copies


class DeskClobberBlocked(RuntimeError):
    """Raised when a write would drastically shrink a substantial file (likely a bug/test)."""


def _bak_dir(p):
    d = p.parent / ".bak"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _count(o):
    try:
        return len(o)
    except TypeError:
        return None


def _rotate_backup(p):
    """Cheap rolling backup: skip when the current content is byte-identical to the last backup (hash),
    or when we backed this file up < _BAK_MIN_INTERVAL ago. Kills the write-amplification of copying a
    ~350 KB file on every 20 s save while still keeping recent recovery points. The atomic temp+replace
    in safe_write_json protects the live file even between backups."""
    if not p.exists():
        return
    try:
        raw = p.read_bytes()
    except OSError:
        return
    h = hashlib.md5(raw).hexdigest()
    last_ts, last_h = _BAK_LAST.get(str(p), (0, None))
    now = time.time()
    if h == last_h or (now - last_ts) < _BAK_MIN_INTERVAL:
        return                                     # unchanged, or too soon since the last rotation
    b = _bak_dir(p) / f"{p.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    try:
        b.write_bytes(raw)
        _BAK_LAST[str(p)] = (now, h)
        for old in sorted(glob.glob(str(_bak_dir(p) / f"{p.name}.*.bak")))[:-_BAK_KEEP]:
            try: os.unlink(old)
            except OSError: pass
    except OSError:
        pass


def safe_write_json(path, obj, *, min_keep_ratio=0.5, floor=20, force=False, indent=None):
    """Atomic, backed-up, clobber-guarded JSON write.
    Tripwire: if the existing file is a readable collection of >= `floor` items and the new payload has
    < `min_keep_ratio` × that, REFUSE — park the candidate at <name>.QUARANTINE, leave the live file
    untouched, raise DeskClobberBlocked. Override with force=True (code that means it) or env
    DESK_ALLOW_SHRINK=1 (operator). min_keep_ratio=0.0 disables the tripwire (for derived/regenerable files)."""
    p = Path(path)
    if p.exists() and not force and not os.environ.get("DESK_ALLOW_SHRINK") and min_keep_ratio > 0:
        try:
            old = json.loads(p.read_text())
            n0, n1 = _count(old), _count(obj)
            if n0 is not None and n1 is not None and n0 >= floor and n1 < n0 * min_keep_ratio:
                q = p.with_suffix(p.suffix + ".QUARANTINE")
                q.write_text(json.dumps(obj, indent=indent))          # keep the candidate; DON'T touch live
                raise DeskClobberBlocked(
                    f"REFUSED shrink of {p.name}: {n0}->{n1} (<{min_keep_ratio:.0%}). "
                    f"Candidate saved to {q.name}. Override: force=True or DESK_ALLOW_SHRINK=1.")
        except (json.JSONDecodeError, OSError):
            pass                                                      # unreadable existing file -> fail-open
    _rotate_backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=indent))
    os.replace(tmp, p)                                                # atomic on same filesystem
