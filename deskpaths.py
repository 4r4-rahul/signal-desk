# deskpaths.py — ONE env-controlled data root, resolved once at import.
# Production (no env set): DATA_DIR == this code dir → byte-identical to legacy behavior.
# Tests set DESK_DATA_DIR=<sandbox> (and DESK_ENV=test) → all DATA I/O redirects to the sandbox,
# so a test can never touch live channels/, the DB, or (via market.connect) the live broker.
import os
from pathlib import Path

CODE = Path(__file__).resolve().parent                       # code assets (dashboard.html, .env) live here
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR", CODE)).resolve()
IS_TEST = os.environ.get("DESK_ENV") == "test"
CHANNELS = DATA_DIR / "channels"
DB_FILE = DATA_DIR / "signal_desk.db"
LOGS = DATA_DIR / "logs"
