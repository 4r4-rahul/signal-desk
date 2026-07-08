/* ============================================================================
 *  Discord Channel History Scraper  (browser console version)
 *  ---------------------------------------------------------------------------
 *  Captures EVERY message in the currently-open channel by auto-scrolling from
 *  the present back to day one. Works even when copy / right-click is disabled,
 *  because the console and file-download are not subject to the page's UI rules.
 *
 *  HOW TO USE
 *  1. Open Discord in your browser:  https://discord.com/app
 *  2. Click into the signals channel you want to capture.
 *  3. Press F12 (or Cmd+Option+I) to open Developer Tools -> "Console" tab.
 *  4. If the console shows a "Don't paste anything here" warning, type the word
 *     'allow pasting' and press Enter (Discord adds this self-XSS guard).
 *  5. Paste this ENTIRE file and press Enter.
 *  6. Leave the window alone. It will scroll upward on its own. When it finishes
 *     it auto-downloads  discord_messages.json  to your Downloads folder.
 *
 *  TUNING (optional): if your connection is slow and it stops too early,
 *  increase WAIT_MS below to 1800 or 2200.
 * ==========================================================================*/
(async () => {
  const WAIT_MS      = 1200;   // how long to wait for older messages to load each scroll
  const STABLE_LIMIT = 10;     // stop after this many scrolls with no new messages (= reached start)
  const MAX_LOOPS    = 100000; // hard safety cap

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // --- locate the messages list and its scroll container ------------------
  const list = document.querySelector('[data-list-id="chat-messages"]');
  if (!list) {
    console.error('❌ Could not find the chat list. Click INTO a channel first, then re-run.');
    return;
  }
  let scroller = list.parentElement;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight) {
    scroller = scroller.parentElement;
  }
  if (!scroller) {
    console.error('❌ Could not find the scroll container.');
    return;
  }

  // --- message collector --------------------------------------------------
  const messages = new Map();   // keyed by DOM message id -> dedupes automatically
  let lastAuthor = null;        // carried forward: grouped messages omit the author header

  const collect = () => {
    document.querySelectorAll('li[id^="chat-messages-"]').forEach((li) => {
      const id        = li.id;
      const contentEl = li.querySelector('[id^="message-content-"]');
      const timeEl    = li.querySelector('time[datetime]');
      const userEl    = li.querySelector('[class*="username"]');
      if (userEl) lastAuthor = userEl.textContent.trim();

      const text = contentEl ? contentEl.innerText.trim() : '';

      // signals are sometimes posted as rich "embeds" rather than plain text
      let embedText = '';
      li.querySelectorAll('[class*="embedField"], [class*="embedDescription"], [class*="embedTitle"]')
        .forEach((e) => { embedText += '\n' + e.innerText.trim(); });

      // reply context: result messages ("Stopped out", "Closed +X") reply to the
      // original entry. The quoted preview text lets us pair result -> entry.
      const replyEl = li.querySelector(
        '[id^="message-reply-context-"], [class*="repliedText"], [class*="repliedMessage"], [class*="replyContext"]'
      );
      const replyTo = replyEl ? replyEl.innerText.trim() : null;

      const ts   = timeEl ? timeEl.getAttribute('datetime') : null;
      const full = (text + (embedText ? '\n[EMBED]' + embedText : '')).trim();

      if (!messages.has(id) && (full || replyTo)) {
        messages.set(id, { id, author: lastAuthor, timestamp: ts, text: full, reply_to: replyTo });
      }
    });
  };

  // --- scroll-up loop -----------------------------------------------------
  console.log('▶ Starting scrape. Do NOT touch this window…');
  let stable = 0, lastCount = 0, loops = 0;

  while (stable < STABLE_LIMIT && loops < MAX_LOOPS) {
    collect();
    scroller.scrollTop = 0;        // jump to top -> triggers Discord to load older messages
    await sleep(WAIT_MS);
    collect();

    const count = messages.size;
    if (count === lastCount) stable++; else stable = 0;
    lastCount = count;
    loops++;

    if (loops % 5 === 0) console.log(`  …collected ${count} messages (${loops} scrolls)`);
  }

  // --- output -------------------------------------------------------------
  const out = Array.from(messages.values())
    .sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));

  console.log(`✅ DONE. Captured ${out.length} messages.`);

  const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = 'discord_messages.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  window.__discordMessages = out;   // also kept in memory as window.__discordMessages
  console.log('📁 discord_messages.json downloaded. Move it into the project "data/" folder.');
})();
