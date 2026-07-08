#!/usr/bin/env python3
"""
Discord Gateway → Signal Desk connector (BOT mode).

Reads .env, connects your bot to Discord, and forwards messages from the
configured channels to the Signal Desk server (which scores + plans them).

.env keys used:
  DISCORD_BOT_TOKEN   your bot token (bot must be in the server, Message
                      Content Intent enabled in the Developer Portal)
  CHANNELS            channelID:provider , comma-separated
                      provider ∈ sulker, mike, optionking, jmt, prince
  SERVER_URL          default http://localhost:8787

Usage:
  python3 gateway.py --test     # post a fake signal (verify the server pipeline)
  python3 gateway.py            # connect the bot and stream live
"""
import os, sys, json, urllib.request
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
SERVER = ENV.get("SERVER_URL", "http://localhost:8787")


def post(author, text, channel=None, provider=None, msg_id=None, reply_to=None):
    body = json.dumps({"author": author, "text": text, "channel": channel,
                       "provider": provider, "msg_id": msg_id, "reply_to": reply_to}).encode()
    try:
        req = urllib.request.Request(SERVER, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        print("  ! could not reach Signal Desk server:", e)
        return False


def parse_channels():
    out = {}
    for pair in (ENV.get("CHANNELS") or "").split(","):
        if ":" in pair:
            cid, lab = pair.split(":", 1)
            out[cid.strip()] = lab.strip()
    return out


def main():
    if "--test" in sys.argv:
        print(f"Posting a test signal to {SERVER} …")
        ok = post("Sulker", "IWM 298P 0DTE @0.64 entry", provider="sulker")
        print("✅ sent — check the dashboard." if ok else "❌ server not running (python3 server.py).")
        return

    token = ENV.get("DISCORD_BOT_TOKEN") or ENV.get("DISCORD_TOKEN")
    if "--list" in sys.argv:                     # list servers/channels the bot can see + their ids
        import discord
        client = discord.Client(intents=discord.Intents.default())

        @client.event
        async def on_ready():
            print(f"Connected as {client.user}. Channels the bot can read:")
            for g in client.guilds:
                print(f"\n  Server: {g.name}  (id {g.id})")
                for ch in g.text_channels:
                    print(f"     #{ch.name}   channel_id = {ch.id}")
            await client.close()

        client.run(token)
        return
    channels = parse_channels()
    if not token:
        sys.exit("❌ No DISCORD_BOT_TOKEN in .env.  Try:  python3 gateway.py --test")
    if not channels:
        sys.exit("❌ No CHANNELS in .env (format: channelID:provider,comma-separated).")

    import discord
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"🟢 Connected as {client.user} — streaming {len(channels)} channels → {SERVER}")
        for cid, lab in channels.items():
            print(f"   {cid} → {lab}")

    @client.event
    async def on_message(m):
        cid = str(m.channel.id)
        if cid in channels and m.content:
            ref = str(m.reference.message_id) if (m.reference and m.reference.message_id) else None
            post(m.author.display_name, m.content, channel=cid, provider=channels[cid],
                 msg_id=str(m.id), reply_to=ref)
            print(f"⚡ {channels[cid]}: {m.content[:70]}" + ("  ↩reply" if ref else ""))

    client.run(token)


if __name__ == "__main__":
    main()
