/* Background service worker: keeps the display awake during scrapes, and relays
   live signals to the local server (background fetch avoids page mixed-content blocks). */
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return;
  if (msg.keepAwake === true) {
    try { chrome.power.requestKeepAwake('display'); } catch (e) {}
  } else if (msg.keepAwake === false) {
    try { chrome.power.releaseKeepAwake(); } catch (e) {}
  } else if (msg.relay) {
    fetch('http://localhost:8787', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(msg.relay),
    }).catch(() => {});   // server may be offline; ignore
  }
});
