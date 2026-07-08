#!/usr/bin/env bash
# Stop the Signal Desk server + bot gateway.
pkill -f "[s]erver.py"  2>/dev/null
pkill -f "[g]ateway.py" 2>/dev/null
lsof -ti:8787 2>/dev/null | xargs kill -9 2>/dev/null
echo "⏹  Signal Desk stopped."
