/* ============================================================================
 *  Universal Chat Scraper — content script (crash-safe, resumable, PER-CHANNEL)
 *  ---------------------------------------------------------------------------
 *  Each Discord channel gets its OWN storage bucket (keyed by the channel id in
 *  the URL), so scraping different channels never mixes their data and each
 *  downloads as its own file. No need to Clear between channels.
 *
 *  - Captures messages while auto-scrolling; writes to IndexedDB in batches
 *    (crash/reload/sleep safe). Re-clicking Start RESUMES that channel.
 *  - Recovers from transient stalls before declaring "reached the end".
 *  - Keeps the display awake via the background worker.
 * ==========================================================================*/
if (!window.__chatScraper) {
  window.__chatScraper = { running: false, seen: new Set(), saved: 0, buffer: [], dbName: null };
  const S = window.__chatScraper;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // --- per-channel key (computed LIVE — Discord is an SPA, channel changes w/o reload)
  const channelKey = () => {
    const m = location.pathname.match(/channels\/(\d+)\/(\d+)/);
    if (m) return `discord_${m[1]}_${m[2]}`;                 // server_channel
    if (location.host.includes('discord.com')) return 'discord_' + location.pathname.replace(/\W+/g, '_');
    return location.host.replace(/\W+/g, '_');
  };
  const dbFor = () => 'chatScraper__' + channelKey();

  // friendly channel name from the page header (NOT the title — Discord's title is "Discord").
  const channelDisplay = () => {
    for (const sel of ['h1', 'header h1', '[class*="title_"] [class*="text"]', '[class*="titleWrapper"]']) {
      const el = document.querySelector(sel);
      const n = el && el.textContent && el.textContent.trim();
      if (n && n.length >= 2 && n.length <= 60 && !/^discord$/i.test(n)) return n;
    }
    return channelKey();
  };

  const setStatus = (txt) => {
    S.status = txt;
    try {
      chrome.storage.local.set({
        scraperStatus: `[${channelKey()}] ${txt}`, scraperCount: S.seen.size,
        scraperSaved: S.saved, scraperRunning: S.running,
      });
    } catch (e) {}
  };

  // ---------------------------------------------------------------- IndexedDB (per channel)
  const STORE = 'messages';
  function openDB(name) {
    return new Promise((res, rej) => {
      const r = indexedDB.open(name, 1);
      r.onupgradeneeded = () => { if (!r.result.objectStoreNames.contains(STORE)) r.result.createObjectStore(STORE, { keyPath: 'id' }); };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  async function idbPutMany(name, items) {
    const db = await openDB(name);
    return new Promise((res, rej) => {
      const tx = db.transaction(STORE, 'readwrite');
      const st = tx.objectStore(STORE);
      items.forEach((it) => st.put(it));
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }
  function idbReq(name, fn) {
    return openDB(name).then((db) => new Promise((res, rej) => {
      const req = fn(db.transaction(STORE, 'readonly').objectStore(STORE));
      req.onsuccess = () => res(req.result || []);
      req.onerror = () => rej(req.error);
    }));
  }
  const idbAllKeys = (name) => idbReq(name, (st) => st.getAllKeys());
  const idbAll = (name) => idbReq(name, (st) => st.getAll());
  async function idbClear(name) {
    const db = await openDB(name);
    return new Promise((res, rej) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).clear();
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  }

  async function flush() {
    if (!S.buffer.length || !S.dbName) return;
    const batch = S.buffer.splice(0, S.buffer.length);
    await idbPutMany(S.dbName, batch);
    S.saved += batch.length;
  }
  const record = (obj) => { if (obj.id && !S.seen.has(obj.id)) { S.seen.add(obj.id); S.buffer.push(obj); } };

  // -------------------------------------------------------------- scrollers
  const findDiscordScroller = () => {
    const list = document.querySelector('[data-list-id="chat-messages"]');
    if (!list) return null;
    let el = list.parentElement;
    while (el && el.scrollHeight <= el.clientHeight) el = el.parentElement;
    return el;
  };
  const findGenericScroller = () => {
    let best = null, bestH = 0;
    document.querySelectorAll('*').forEach((el) => {
      const s = getComputedStyle(el);
      if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 200 && el.scrollHeight > bestH) {
        bestH = el.scrollHeight; best = el;
      }
    });
    return best || document.scrollingElement || document.documentElement;
  };

  // -------------------------------------------------------------- collectors
  let lastAuthor = null;
  const collectDiscord = () => {
    document.querySelectorAll('li[id^="chat-messages-"]').forEach((li) => {
      const contentEl = li.querySelector('[id^="message-content-"]');
      const timeEl = li.querySelector('time[datetime]');
      const userEl = li.querySelector('[class*="username"]');
      if (userEl) lastAuthor = userEl.textContent.trim();
      const text = contentEl ? contentEl.innerText.trim() : '';
      let embedText = '';
      li.querySelectorAll('[class*="embedField"], [class*="embedDescription"], [class*="embedTitle"]')
        .forEach((e) => { embedText += '\n' + e.innerText.trim(); });
      const replyEl = li.querySelector('[id^="message-reply-context-"], [class*="repliedText"], [class*="repliedMessage"], [class*="replyContext"]');
      const replyTo = replyEl ? replyEl.innerText.trim() : null;
      const ts = timeEl ? timeEl.getAttribute('datetime') : null;
      const full = (text + (embedText ? '\n[EMBED]' + embedText : '')).trim();
      if (full || replyTo) record({ id: li.id, author: lastAuthor, timestamp: ts, text: full, reply_to: replyTo });
    });
  };
  const hash = (str) => { let h = 0; for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0; return 'g' + (h >>> 0); };
  const collectGeneric = () => {
    document.querySelectorAll('li, [role="listitem"], article, [class*="message"], [class*="Message"], [class*="item"], [class*="post"], [class*="tweet"]')
      .forEach((el) => {
        const text = (el.innerText || '').trim();
        if (!text || text.length < 2) return;
        const timeEl = el.querySelector('time[datetime]');
        record({ id: hash(text), author: null, timestamp: timeEl ? timeEl.getAttribute('datetime') : null, text, reply_to: null });
      });
  };

  // -------------------------------------------------------------- main loop
  async function startScrape(opts) {
    if (S.running) { setStatus('Already running…'); return; }
    const isDiscord = location.host.includes('discord.com');
    const mode = (opts.mode && opts.mode !== 'auto') ? opts.mode : (isDiscord ? 'discord' : 'generic');
    const waitMs = opts.waitMs || 1200;
    const stableLimit = opts.stableLimit || 10;
    const direction = opts.direction || 'up';

    const scroller = mode === 'discord' ? findDiscordScroller() : findGenericScroller();
    if (!scroller) { setStatus('❌ No scroll area found. Click into the channel/feed first.'); return; }
    const collect = mode === 'discord' ? collectDiscord : collectGeneric;

    // bind to THIS channel's own database and resume from it
    S.dbName = dbFor();
    S.seen = new Set();
    try {
      const keys = await idbAllKeys(S.dbName);
      keys.forEach((k) => S.seen.add(k));
      S.saved = keys.length;
    } catch (e) { S.saved = 0; }

    S.running = true;
    try { chrome.runtime.sendMessage({ keepAwake: true }); } catch (e) {}
    setStatus(`▶ Started (${mode}, ${direction}). This channel has ${S.saved} saved…`);

    // Discord VIRTUALIZES the message list (only ~100 rows in the DOM at once and
    // recycles them), so page-height never grows. The only reliable progress signal
    // is "are we capturing NEW message ids?". When we stall at Discord's loading
    // frontier, a firm jiggle (jump down a few screens, then snap to the top) forces
    // it to fetch older history. We get more patient (backoff) before giving up.
    let stable = 0, loops = 0, backoff = 0;
    const MAX_LOOPS = 5000000;
    const up = direction === 'up';
    const vh = () => scroller.clientHeight || 600;

    while (S.running && stable < stableLimit && loops < MAX_LOOPS) {
      collect();
      const beforeSeen = S.seen.size;
      const beforeTop = scroller.scrollTop;

      // normal step toward the edge
      scroller.scrollTop = up ? Math.max(0, beforeTop - vh() * 1.2)
                              : Math.min(scroller.scrollHeight, beforeTop + vh() * 1.2);
      await sleep(Math.max(400, Math.round(waitMs * 0.5)));
      collect();

      const moved = up ? (scroller.scrollTop < beforeTop - 4) : (scroller.scrollTop > beforeTop + 4);
      const gotNew = S.seen.size > beforeSeen;

      if (gotNew || moved) {
        stable = 0; backoff = 0;                       // progressing through history
      } else {
        // at the frontier and nothing new — jiggle to unstick Discord's lazy loader
        scroller.scrollTop = up ? vh() * 5 : Math.max(0, scroller.scrollHeight - vh() * 5);
        await sleep(500);
        scroller.scrollTop = up ? 0 : scroller.scrollHeight;
        await sleep(waitMs + backoff);                 // wait longer each consecutive stall
        collect();
        if (S.seen.size > beforeSeen) { stable = 0; backoff = 0; }
        else { stable++; backoff = Math.min(backoff + 1200, 15000); }
      }
      loops++;

      if (S.buffer.length >= 150) await flush();
      setStatus(`${S.running ? '▶' : '⏹'} ${S.seen.size} captured · ${S.saved + S.buffer.length} saved · ${loops} scrolls · stall ${stable}/${stableLimit}`);
    }

    await flush();
    const finished = stable >= stableLimit;
    S.running = false;
    try { chrome.runtime.sendMessage({ keepAwake: false }); } catch (e) {}
    setStatus(`✅ ${finished ? 'Reached the beginning.' : 'Stopped.'} ${S.seen.size} captured (${S.saved} saved). Click "Download JSON".`);
    if (finished) downloadData();
  }

  async function downloadData() {
    await flush();
    const dbName = S.dbName || dbFor();          // current channel
    const out = (await idbAll(dbName)).sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
    const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${channelKey()}_messages.json`;   // filename includes channel id
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    setStatus(`📁 Downloaded ${out.length} messages as ${channelKey()}_messages.json`);
  }

  async function clearData() {
    S.running = false;
    const dbName = dbFor();                       // clears ONLY the current channel
    await idbClear(dbName);
    if (S.dbName === dbName) { S.seen = new Set(); S.saved = 0; S.buffer = []; }
    setStatus('🗑️ Cleared THIS channel\'s data. Other channels untouched.');
  }

  // ---------------------------------------------------------- LIVE WATCH
  // Detect NEW signal messages and relay them to the local server (localhost:8787).
  S.watchSeen = new Set();
  function startWatch() {
    if (S.watching) { setStatus('👀 already watching'); return; }
    const list = document.querySelector('[data-list-id="chat-messages"]');
    if (!list) { setStatus('❌ Open the channel first, then Watch.'); return; }
    const scan = (li) => {
      if (!li.id || !li.id.startsWith('chat-messages-') || S.watchSeen.has(li.id)) return;
      const timeEl = li.querySelector('time[datetime]');
      if (timeEl) {   // only fresh posts (< 10 min old) — ignore history rendered on scroll
        const age = (Date.now() - new Date(timeEl.getAttribute('datetime')).getTime()) / 60000;
        if (age > 10) return;
      }
      const userEl = li.querySelector('[class*="username"]');
      if (userEl) lastAuthor = userEl.textContent.trim();
      const contentEl = li.querySelector('[id^="message-content-"]');
      const text = contentEl ? contentEl.innerText.trim() : '';
      // capture the reply preview (parent's full text) + this message's id — for cross-day tagging
      const replyEl = li.querySelector('[id^="message-reply-context-"], [class*="repliedText"], [class*="repliedMessage"], [class*="replyContext"]');
      const replyTo = replyEl ? replyEl.innerText.trim() : null;
      // relay if it looks like a signal OR it's a reply/management update (don't drop "+30% trim half")
      const isSignal = /\b\d{2,5}(?:\.\d)?\s*(?:[cp]\b|call|put)/i.test(text);
      const isMgmt = !!replyTo || /\d|half|trim|out\b|cut|stop|runner|secure|lock|flat|close|exit|sold|off\b/i.test(text);
      if (!isSignal && !isMgmt) return;
      S.watchSeen.add(li.id);
      try { chrome.runtime.sendMessage({ relay: { author: lastAuthor, text, reply_to: replyTo, msg_id: li.id, channel: channelKey(), name: channelDisplay() } }); } catch (e) {}
      setStatus('⚡ routed: ' + text.slice(0, 42));
    };
    S.observer = new MutationObserver((muts) => {
      muts.forEach((m) => m.addedNodes.forEach((nd) => {
        if (nd.nodeType !== 1) return;
        if (nd.matches && nd.matches('li[id^="chat-messages-"]')) scan(nd);
        if (nd.querySelectorAll) nd.querySelectorAll('li[id^="chat-messages-"]').forEach(scan);
      }));
    });
    S.observer.observe(list, { childList: true, subtree: true });
    S.watching = true;
    // SAFETY RE-SCAN: the observer can miss a message if the tab was momentarily occluded/frozen,
    // scrolled up, or the post was an edit. Re-scan the last ~25 visible messages each heartbeat;
    // scan()'s <10-min age filter + watchSeen dedupe mean only genuinely-missed recent ones relay.
    const rescan = () => {
      const el = document.querySelector('[data-list-id="chat-messages"]');
      if (el) Array.from(el.querySelectorAll('li[id^="chat-messages-"]')).slice(-25).forEach(scan);
    };
    // heartbeat so the dashboard shows this window as "listening" even before a signal
    const beat = () => { rescan(); try { chrome.runtime.sendMessage({ relay: { watching: true, channel: channelKey(), name: channelDisplay() } }); } catch (e) {} };
    beat();
    S.beat = setInterval(beat, 20000);
    setStatus('👀 Watching ' + channelDisplay() + ' — new signals route to localhost:8787');
  }
  function stopWatch() {
    S.watching = false;
    if (S.observer) { S.observer.disconnect(); S.observer = null; }
    if (S.beat) { clearInterval(S.beat); S.beat = null; }
    setStatus('⏹ Watch stopped.');
  }

  chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    if (msg.cmd === 'start') startScrape(msg.opts || {});
    else if (msg.cmd === 'stop') { S.running = false; setStatus('⏹ Stopping…'); }
    else if (msg.cmd === 'download') downloadData();
    else if (msg.cmd === 'clear') clearData();
    else if (msg.cmd === 'watch') startWatch();
    else if (msg.cmd === 'unwatch') stopWatch();
    if (reply) reply({ ok: true, count: S.seen.size, saved: S.saved, running: S.running });
    return true;
  });

  // On (re)injection, report what's already saved for the CURRENT channel.
  idbAllKeys(dbFor()).then((keys) => setStatus(`Ready. ${keys.length} messages saved for this channel.`)).catch(() => setStatus('Ready.'));
}
