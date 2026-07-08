#!/usr/bin/env python3
"""
Load scraped Discord messages into a per-channel SQLite DB.

Usage:
    python3 load.py <channel> [path/to/messages.json]

Stores into  channels/<channel>/<channel>.db  (raw_messages table). The default
JSON is  channels/<channel>/messages.json. Safe to re-run: existing messages
(matched by id) are skipped, so you can re-scrape and top up.
"""
import json
import sqlite3
import sys
from pathlib import Path

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "jmt"
CH_DIR = Path(__file__).parent / "channels" / CHANNEL
DB_PATH = CH_DIR / f"{CHANNEL}.db"
DEFAULT_JSON = CH_DIR / "messages.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id        TEXT PRIMARY KEY,   -- Discord DOM message id (dedupe key)
    author    TEXT,
    timestamp TEXT,               -- ISO 8601
    text      TEXT,
    reply_to  TEXT                -- quoted preview of the message this replies to (for pairing results->entries)
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_messages(timestamp);
"""


def main():
    json_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_JSON
    CH_DIR.mkdir(parents=True, exist_ok=True)
    if not json_path.exists():
        sys.exit(f"❌ Not found: {json_path}\n   Run the scraper first, then move the "
                 f"downloaded discord_messages.json into the data/ folder.")

    messages = json.loads(json_path.read_text(encoding="utf-8"))
    print(f"Read {len(messages)} messages from {json_path.name}")

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    before = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO raw_messages (id, author, timestamp, text, reply_to) VALUES (?,?,?,?,?)",
        [(m.get("id"), m.get("author"), m.get("timestamp"), m.get("text"), m.get("reply_to")) for m in messages],
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]

    print(f"✅ Inserted {after - before} new messages (DB now holds {after} total).")

    # quick sanity peek
    rng = conn.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM raw_messages WHERE timestamp IS NOT NULL"
    ).fetchone()
    print(f"   Date range: {rng[0]}  →  {rng[1]}")
    print("\n   Authors seen:")
    for author, n in conn.execute(
        "SELECT author, COUNT(*) FROM raw_messages GROUP BY author ORDER BY 2 DESC LIMIT 10"
    ):
        print(f"     {n:6d}  {author}")
    conn.close()
    print(f"\nDB written to {DB_PATH}")


if __name__ == "__main__":
    main()
