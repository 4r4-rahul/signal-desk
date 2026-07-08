#!/usr/bin/env python3
"""
Integration layer + tester for the downstream trade loop.
  - send_alert(text)   → posts to your Discord ALERT_WEBHOOK_URL
  - ibkr_check()       → checks Interactive Brokers API port (TWS / IB Gateway)
  - request_approval() → (stub) DM-approval flow via the bot (needs gateway running)

Run:  python3 integrations.py            # tests webhook + IBKR
      python3 integrations.py --no-webhook
Secrets are read from .env at runtime and never printed.
"""
import os, sys, json, socket, urllib.request
from pathlib import Path


def load_env(p=".env"):
    env = {}
    if Path(p).exists():
        for ln in Path(p).read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()


def send_alert(text, title="⚡ Signal Desk"):
    url = ENV.get("ALERT_WEBHOOK_URL")
    if not url:
        return False, "no ALERT_WEBHOOK_URL in .env"
    payload = {"embeds": [{"title": title, "description": text, "color": 3066993}]}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "SignalDesk/1.0 (+https://localhost)"})
        code = urllib.request.urlopen(req, timeout=8).status
        return (200 <= code < 300), f"HTTP {code}"
    except Exception as e:
        return False, str(e)


def ibkr_check():
    host = ENV.get("IBKR_HOST", "127.0.0.1")
    port = int(ENV.get("IBKR_PORT", "7497"))
    try:
        with socket.create_connection((host, port), timeout=3):
            kind = {7497: "TWS paper", 7496: "TWS live", 4002: "IB Gateway paper", 4001: "IB Gateway live"}.get(port, "?")
            return True, f"{host}:{port} reachable ({kind}) — API socket is open"
    except Exception as e:
        return False, f"{host}:{port} not reachable ({e}) — is TWS/IB Gateway running with API enabled?"


def main():
    print("=" * 58)
    print("INTEGRATION TESTS")
    print("=" * 58)

    print("\n[1] Discord webhook alert")
    if "--no-webhook" in sys.argv:
        print("    skipped (--no-webhook)")
    else:
        ok, msg = send_alert(
            "**Integration test ✅**\nYour webhook is connected.\n\n"
            "Example: **IWM 298P** 0DTE @ $0.64 — confidence **93/100 HIGH**\n"
            "Plan: 5 contracts · stop $0.35 · scale 3 @ $0.90 · 2 runners",
            title="⚡ Signal Desk — test")
        print(f"    {'✅ posted — check your Discord channel' if ok else '❌ failed'}  ({msg})")

    print("\n[2] Interactive Brokers API port")
    ok, msg = ibkr_check()
    print(f"    {'✅' if ok else '❌'} {msg}")

    print("\n[3] Signal Desk server")
    try:
        urllib.request.urlopen(ENV.get("SERVER_URL", "http://localhost:8787") + "/api/signals", timeout=4)
        print("    ✅ Signal Desk reachable")
    except Exception as e:
        print(f"    ❌ Signal Desk not running ({e}) — run: python3 server.py")


if __name__ == "__main__":
    main()
