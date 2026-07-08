# Live Signals — Discord → your app → trade plan

Real-time pipeline: a provider posts a signal → the extension detects it → routes to a local
server → the **signal engine** scores confidence and prints a full trade plan in your terminal.

```
Discord post  →  Extension "Watch"  →  background fetch  →  server.py (localhost:8787)
                                                                 → signal_engine.py
                                                                 → trade plan in terminal + live_signals.md
```

## One-time setup
Reload the extension after this update (it added the Watch feature + localhost permission):
`chrome://extensions` → ↻ reload on **Universal Chat Scraper** (accept the new localhost permission).

## Each session
1. **Start the server** in a VSCode terminal:
   ```
   python3 server.py
   ```
2. **Open the dashboard:** browse to **http://localhost:8787** — the Signal Desk UI.
3. **Open the provider's channel** in Discord (e.g. sulker-options), click into it.
4. In the extension popup, click **👀 Watch (live signals)**.
5. Done. When Sulker/Mike posts a signal, a **card appears live on the dashboard** with a
   confidence ring + full plan, and **Take / Skip** buttons that log straight to the tracker.
   (Sizing: `ACCOUNT=15000 RISK_PCT=1 python3 server.py`.)

## What you get per signal
```
⚡ 16:45:58  LIVE SIGNAL (sulker)
   IWM 298P  0DTE  @$0.64
   CONFIDENCE 93/100  →  HIGH ✅
   provider sulker (+70) · ticker IWM (+15) · liquid $0.64 (+8)
   evidence: IWM history: 89% win, median +70% (n=91, self-reported)
   Contracts   : 5   Risk $144 (1.0%)   STOP $0.35
   SCALE OUT   : sell 3 at $0.90 (+40%)   RUNNER: keep 2, stop→BE, trail/+75%/EOD
```

## Watch MULTIPLE channels at once
Discord web only renders one channel per tab, so fan them in:
1. Open **each** provider channel in its **own browser tab** (Sulker in one, Mike in another, …).
2. Click **👀 Watch** in the extension **in each tab**.
3. All of them stream into the **same dashboard** — the "Live channels" strip shows each source
   (click a chip to filter that provider in/out). The server auto-detects the provider by author.
- Keep the tabs in visible windows for lowest latency (background tabs can lag a few seconds).
- Want zero-tab, instant, all-channels streaming? That's the **gateway websocket** upgrade
  (uses your Discord token — ToS/ban risk). Ask and I'll build it.

## Test without waiting for a post
```
python3 signal_engine.py "sulker: IWM 298P 0DTE @0.64"     # engine only
# or simulate the live path:
curl -s -X POST localhost:8787 -H "Content-Type: application/json" \
  -d '{"author":"Sulker","text":"IWM 298P 0DTE @0.64"}'
```

## Honest limits (read once)
- **"Confidence" is a rules-based QUALITY score** (provider trust + ticker/premium/type edge from
  history), NOT a prediction that this trade wins — we proved individual outcomes aren't
  predictable. It enforces *consistency*, it doesn't foresee the future.
- The win%/median shown are **self-reported historical** figures (optimistic ceiling).
- The plan is a **decision-support tool** — it does NOT place orders. You review and execute.
- Tune sizing with env vars: `ACCOUNT=15000 RISK_PCT=1 python3 server.py`.
- After each trade, log your real fill: `python3 tracker.py sulker take/close ...` — that's what
  turns this into YOUR measured edge over time.
```
