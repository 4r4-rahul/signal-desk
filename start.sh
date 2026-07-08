#!/usr/bin/env bash
# Signal Desk launcher — boots the dashboard server + the Discord bot gateway together.
# Usage:  ./start.sh          (Ctrl+C stops both)
# Config: ACCOUNT, RISK_PCT, EQUITY_SOURCE, ENRICH_IBKR can be set as env vars.

cd "$(dirname "$0")"
PY=.venv/bin/python
export ACCOUNT="${ACCOUNT:-15000}"
export ENRICH_IBKR="${ENRICH_IBKR:-1}"
export EQUITY_SOURCE="${EQUITY_SOURCE:-allocation}"
mkdir -p logs

echo "⏹  clearing any old processes…"
pkill -f "[s]erver.py"  2>/dev/null
pkill -f "[g]ateway.py" 2>/dev/null
lsof -ti:8787 2>/dev/null | xargs kill -9 2>/dev/null
sleep 1

echo "▶  starting Signal Desk server (account \$$ACCOUNT, $EQUITY_SOURCE compounding)…"
$PY server.py > logs/server.log 2>&1 &
SRV=$!
for i in $(seq 1 20); do curl -s localhost:8787/api/signals >/dev/null 2>&1 && break; sleep 0.5; done

echo "▶  starting Discord bot gateway…"
$PY gateway.py > logs/gateway.log 2>&1 &
GW=$!
sleep 3

echo ""
echo "════════════════════════════════════════════════════════"
echo "  ✅  Signal Desk is LIVE  →  http://localhost:8787"
echo "      server pid $SRV · gateway pid $GW · logs in ./logs/"
echo "      Ctrl+C to stop both."
echo "════════════════════════════════════════════════════════"
echo ""

cleanup() { echo; echo "⏹  stopping…"; kill "$SRV" "$GW" 2>/dev/null;
            lsof -ti:8787 2>/dev/null | xargs kill -9 2>/dev/null; exit 0; }
trap cleanup INT TERM

# stream both logs so you see live activity (signals, alerts, bot events)
tail -f logs/server.log logs/gateway.log
