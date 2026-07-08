# Forward Tracker — daily workflow

Goal: capture Mike's LIVE alerts (including the ~56% of days he never recaps) and log
YOUR actual fills, so you learn your REAL edge instead of his quoted one.

## Each trading day

1. **Refresh the data.** Open mike-scalps in the browser, run the scraper briefly (Start →
   let it grab the new messages at the top → Stop), Download, then:
   ```
   cp ~/Downloads/discord_979306667319656479_1337877131975463085_messages*.json channels/mike/messages.json
   python3 load.py mike channels/mike/messages.json
   ```

2. **Pull new alerts into your journal:**
   ```
   python3 tracker.py mike sync
   python3 tracker.py mike list          # shows OPEN alerts (🎰 = lotto)
   ```

3. **As you trade, record what YOU actually did** (use the 6-char handle from `list`):
   ```
   python3 tracker.py mike take 394758 1.05      # you entered at your real fill 1.05
   python3 tracker.py mike close 394758 1.40     # you exited at 1.40
   python3 tracker.py mike close 394758 --pct 35 # ...or just log the % if you don't have prices
   python3 tracker.py mike skip 287626           # you chose not to take it
   ```

4. **Check your honest scorecard anytime:**
   ```
   python3 tracker.py mike score
   ```
   It shows YOUR win rate, median, normal-vs-lotto split, your entry slippage vs Mike's
   quote, and — the key line — **your edge vs his quotes** (the real cost of following).

## Rules to hold yourself to (from REPORT.md)
- Normal liquid scalps at ~1%; lottos ¼-size, house-money, EOD-only, 1–2/day.
- Limit orders within ~10% of Mike's price; can't fill → **skip** (log it as skip).
- Scale out ⅔ at +30–50%, stop −45%. Don't hold for the moon.
- Daily stop: quit after −3% or 4 losses in a row.

## After 2–4 weeks
Run `score`. If your real expectancy is clearly positive **after your actual fills**, scale up.
If it's near zero or negative, the follower slippage is eating the edge — don't add size.

Notes:
- The journal is `channels/mike/journal.csv` — you can also edit it by hand in Excel/Numbers.
- `sync` only ADDS new alerts; it never overwrites your entries. Safe to run daily.
