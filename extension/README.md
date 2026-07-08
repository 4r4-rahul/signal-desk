# Universal Chat Scraper — install & use

A one-click Chrome/Edge/Brave extension that auto-scrolls a chat or feed and saves
the full history to a JSON file. Discord-aware (captures author, timestamp, message
text, and reply-links); generic mode works on most other sites.

## Install (one time, ~1 minute)

1. Open your browser and go to:
   - Chrome:  `chrome://extensions`
   - Edge:    `edge://extensions`
   - Brave:   `brave://extensions`
2. Turn on **Developer mode** (top-right toggle).
3. Click **Load unpacked**.
4. Select this folder:
   `/Users/rahul/Workspace/Copy trading/extension`
5. The "Universal Chat Scraper" icon appears in your toolbar (pin it for convenience).

## Use

1. Open the chat/feed in a browser tab (e.g. the Discord channel) and **click into it**.
2. Click the extension icon → set **Mode** (Auto is fine for Discord) and
   **Scroll direction** (Up = chat history, Down = feeds).
3. Click **Start**. It scrolls on its own; the status line shows the running count.
   You can close the popup — it keeps going. Reopen it anytime to see progress.
4. When it reaches the beginning it **auto-downloads** `<site>_messages.json`.
   (You can also click **Download JSON** anytime to grab what's captured so far.)
5. Move the downloaded JSON into this project's `data/` folder.

## Surviving a long (multi-year) scrape — IMPORTANT
- **Nothing is lost on a crash.** Messages are written to the browser's on-disk
  database (IndexedDB) in batches as they're captured. If the tab crashes, Discord
  reloads, or your Mac sleeps, just open the channel again and click **Start** — it
  **resumes** and dedupes against what's already saved.
- **Keep-awake** is automatic: while scraping, the extension stops the display/system
  from sleeping. (Still, plug in your laptop for a long run.)
- **Keep the tab focused** — background tabs get throttled by the browser, which slows
  capture dramatically. Best: leave it as the active tab and don't minimize.
- If it ever stalls, it auto-recovers (scroll-jiggle + longer wait) before deciding
  it has reached the channel's beginning.
- Grab interim results anytime with **Download JSON** — even mid-run.

## Switching channels
- Click **Clear** to wipe the saved data before scraping a *different* channel,
  otherwise the new capture is added on top of the old one.

## Notes
- Re-running resumes/top-ups from the on-disk data (safe).
- If it stops too early on a slow connection, raise **Wait** to 2000 and Start again.
- No icons are bundled, so the browser shows a default puzzle-piece icon — that's normal.
