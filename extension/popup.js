const $ = (id) => document.getElementById(id);

// Always target the Discord tab if one is open, regardless of which tab is
// active when you click. Falls back to the active tab for generic sites.
async function targetTab() {
  // 1) Prefer the Discord tab in the window this popup was opened from — so Watch
  //    controls THIS window's channel (multi-window support).
  const [cur] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (cur && /:\/\/([^/]*\.)?discord\.com\//.test(cur.url || '')) return cur;
  // 2) Fallback: any Discord tab (lets you also drive it from the extensions page).
  const discord = await chrome.tabs.query({ url: ['*://*.discord.com/*'] });
  if (discord.length) return discord.find((t) => t.active) || discord[discord.length - 1];
  // 3) Generic site in the current window.
  if (cur && /^https?:/.test(cur.url || '')) return cur;
  return null;
}

async function ensureInjected(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
}

async function withTab(fn, needConfirm) {
  const tab = await targetTab();
  if (!tab) { $('status').textContent = '❌ No Discord tab found. Open the channel in a tab first.'; return; }
  try {
    await ensureInjected(tab.id);
    await fn(tab);
  } catch (e) {
    $('status').textContent = '❌ ' + e.message + ' — reload the Discord page, then try again.';
  }
}

$('start').onclick = () => withTab(async (tab) => {
  const opts = {
    mode: $('mode').value,
    direction: $('dir').value,
    waitMs: parseInt($('wait').value, 10) || 1200,
    stableLimit: parseInt($('stable').value, 10) || 10,
  };
  await chrome.tabs.sendMessage(tab.id, { cmd: 'start', opts });
  $('status').textContent = `▶ Started on: ${tab.title || tab.url}`;
});

$('stop').onclick = () => withTab(async (tab) => { await chrome.tabs.sendMessage(tab.id, { cmd: 'stop' }); });

$('download').onclick = () => withTab(async (tab) => { await chrome.tabs.sendMessage(tab.id, { cmd: 'download' }); });

$('clear').onclick = () => {
  if (!confirm('Delete all captured messages saved on disk for this site? (Use this before scraping a different channel.)')) return;
  withTab(async (tab) => { await chrome.tabs.sendMessage(tab.id, { cmd: 'clear' }); });
};

$('watch').onclick = () => withTab(async (tab) => { await chrome.tabs.sendMessage(tab.id, { cmd: 'watch' }); });
$('unwatch').onclick = () => withTab(async (tab) => { await chrome.tabs.sendMessage(tab.id, { cmd: 'unwatch' }); });

// Live status: poll storage (survives popup close/reopen).
setInterval(async () => {
  const { scraperStatus } = await chrome.storage.local.get('scraperStatus');
  if (scraperStatus) $('status').textContent = scraperStatus;
}, 500);
