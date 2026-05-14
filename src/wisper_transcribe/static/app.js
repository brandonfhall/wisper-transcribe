/* wisper-transcribe web UI — vanilla JS
   Handles: SSE record meters, ticker, file upload feedback.
   HTMX handles all partial page updates and polling.
*/

// ── Record: per-speaker audio-level meters ──
// Called by the inline SSE handler in record.html with voice_activity event data.
// data: { speakers: { uid: { level: 0-1, is_speaking: bool } } }
window.wisperUpdateMeters = function(data) {
  var speakers = data.speakers || data;
  Object.keys(speakers).forEach(function(uid) {
    var s = speakers[uid];
    var container = document.getElementById('meter-' + uid);
    if (!container) return;

    var bars = container.querySelectorAll('.meter-bar');
    var isSpeaking = s.is_speaking || s.isSpeaking || false;
    var level = Math.min(1, Math.max(0, s.level || 0));

    bars.forEach(function(bar, i) {
      var phase = (i / bars.length) * Math.PI * 2;
      var base = 0.25 + 0.45 * Math.abs(Math.sin(phase * 1.3 + i * 0.4));
      var h = isSpeaking
        ? Math.max(0.08, base * (0.55 + level * 0.7))
        : Math.max(0.04, 0.18 * (0.3 + level));

      bar.style.height = Math.round(h * 100) + '%';
      if (isSpeaking) {
        bar.style.background = 'linear-gradient(180deg, #5fd4e7, #5fd4e755)';
        bar.style.boxShadow = h > 0.5 ? '0 0 6px #5fd4e780' : 'none';
      } else {
        bar.style.background = 'rgba(243,234,216,0.18)';
        bar.style.boxShadow = 'none';
      }
    });

    // Update row styling
    var row = container.closest('.speaker-row');
    if (row) {
      row.style.background = isSpeaking
        ? 'linear-gradient(90deg, #5fd4e706, transparent 60%)'
        : 'transparent';
      var avatar = row.querySelector('.speaker-avatar');
      if (avatar) {
        avatar.style.boxShadow = isSpeaking
          ? '0 0 0 2px #5fd4e780, 0 0 14px #5fd4e740'
          : 'none';
      }
      var pill = row.querySelector('.live-pill');
      if (pill) {
        if (isSpeaking) {
          pill.textContent = 'LIVE';
          pill.style.background = '#5fd4e718';
          pill.style.border = '1px solid #5fd4e750';
          pill.style.color = '#5fd4e7';
        } else {
          pill.textContent = 'QUIET';
          pill.style.background = 'transparent';
          pill.style.border = '1px solid rgba(243,234,216,0.09)';
          pill.style.color = '#5f6571';
        }
      }
      var talkTime = row.querySelector('.talk-time');
      if (talkTime) {
        talkTime.style.color = isSpeaking ? '#5fd4e7' : '#a3a89e';
      }
    }
  });
};

// ── Record: live transcript ticker ──
// Called by the SSE handler in record.html with partial_transcript event data.
// data: { timestamp: "01:24:09", speaker: "Alice", text: "..." }
window.wisperTickerAppend = function(data) {
  var ticker = document.getElementById('live-ticker');
  if (!ticker) return;

  // Remove placeholder if present
  var placeholder = ticker.querySelector('div[style*="font-style"]');
  if (placeholder) placeholder.remove();

  var line = document.createElement('div');
  line.style.cssText = 'display:grid;grid-template-columns:60px 90px 1fr;gap:14px;padding:6px 0;align-items:baseline;opacity:1';
  line.innerHTML =
    '<span style="font-family:var(--font-mono);font-size:10.5px;color:var(--color-paper-faint)">' +
      (data.timestamp || '—') +
    '</span>' +
    '<span style="display:flex;align-items:center;gap:7px">' +
      '<span style="width:6px;height:6px;border-radius:50%;background:var(--color-accent);box-shadow:0 0 6px var(--color-accent);flex-shrink:0"></span>' +
      '<span style="font-family:var(--font-serif);font-size:13px;color:var(--color-paper)">' + (data.speaker || '') + '</span>' +
    '</span>' +
    '<span style="font-size:13.5px;color:var(--color-paper);line-height:1.5">' + (data.text || '') + '</span>';

  // Prepend (newest at top) and fade older lines
  ticker.insertBefore(line, ticker.firstChild);

  // Fade older entries
  var lines = ticker.querySelectorAll('div[style*="grid-template-columns"]');
  lines.forEach(function(l, i) {
    l.style.opacity = Math.max(0.45, 1 - i * 0.12);
  });

  // Keep at most 12 lines
  while (lines.length > 12) {
    ticker.removeChild(ticker.lastChild);
  }
};

// ── File upload feedback ──
document.addEventListener('DOMContentLoaded', function() {
  // Auto-scroll any log terminal
  var terminal = document.getElementById('log-terminal');
  if (terminal) terminal.scrollTop = terminal.scrollHeight;
});

// ── Sidebar status fallback ──
// htmx handles this via hx-trigger="load, every 5s" when it's available.
// If htmx.min.js is still the placeholder (local dev), this vanilla-JS
// fallback fires instead so the Device / Jobs cells are never blank.
(function() {
  var wrap = document.getElementById('sidebar-status-wrap');
  if (!wrap) return;

  function pollSidebarStatus() {
    fetch('/api/sidebar-status')
      .then(function(r) { return r.text(); })
      .then(function(html) { wrap.innerHTML = html; })
      .catch(function() {});
  }

  // Only activate if htmx hasn't already claimed the element
  // (htmx sets 'data-hx-processed' on elements it manages).
  setTimeout(function() {
    if (!wrap.hasAttribute('data-hx-processed')) {
      pollSidebarStatus();
      setInterval(pollSidebarStatus, 5000);
    }
  }, 200);
})();
