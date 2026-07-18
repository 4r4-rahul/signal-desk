#!/usr/bin/env python3
"""Rebuild channels/_signals.json from the append-only live_signals.jsonl archives — the recovery that
worked after the clobber incident. Read-only unless --write. Never touches the running server.

Usage:
  .venv/bin/python restore_signals.py            # dry-run: report the reconstructed count
  .venv/bin/python restore_signals.py --write     # apply (routes through safe_io with force=True)
"""
import json, glob, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def rebuild():
    latest = {}                                              # id -> (ts_utc, record); keep newest per id
    for f in sorted(glob.glob(str(ROOT / "channels" / "*" / "live_signals.jsonl"))):
        for ln in Path(f).read_text().splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            i, ts = r.get("id"), r.get("ts_utc", "")
            if not i or str(i).startswith("TEST"):           # never resurrect a test artifact
                continue
            if i not in latest or ts >= latest[i][0]:
                latest[i] = (ts, r)
    cards = [r for _, r in sorted(latest.values(), key=lambda x: x[0])]
    dropped = [c for c in cards if c.get("status") == "DELETED"]     # honor EDIT-delete tombstones
    cards = [c for c in cards if c.get("status") != "DELETED"]
    for c in cards:                                          # reconstruct the tape blob the dashboard needs
        if "tech" not in c:
            c["tech"] = {"label": c.get("tech_label"), "ind": c.get("indicators")}
    return cards[-250:], len(dropped)


if __name__ == "__main__":
    cards, tomb = rebuild()
    cur = ROOT / "channels" / "_signals.json"
    n_old = len(json.loads(cur.read_text())) if cur.exists() else 0
    print(f"reconstructed {len(cards)} cards (dropped {tomb} tombstoned) | current file has {n_old}")
    if "--write" in sys.argv:
        from safe_io import safe_write_json
        safe_write_json(cur, cards, force=True)              # explicit human recovery action = force
        print("written to channels/_signals.json")
    else:
        print("dry-run — re-run with --write to apply.")
