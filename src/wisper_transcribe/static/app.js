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

// ── Record: live-transcript SSE connector ──
// Shared by record.html's ticker and recording_detail.html's pane.
// The server's resume cursor is per connection, so any reconnect replays the
// full history; lines are de-duped on raw `start_s|speaker|text`.
window.wisperConnectLiveStream = function(url, onLine, onSnapshot) {
  var seen = new Set();
  try {
    var src = new EventSource(url);
    src.onmessage = function(e) {
      try {
        var payload = JSON.parse(e.data);
        if (payload.type === 'line') {
          var key = payload.start_s + '|' + payload.speaker + '|' + payload.text;
          if (seen.has(key)) return;
          seen.add(key);
          onLine(payload);
        } else if (payload.type === 'snapshot' && onSnapshot) {
          onSnapshot(payload);
        }
      } catch (ex) {}
    };
    src.addEventListener('end', function() { src.close(); });
    src.onerror = function() { src.close(); };
    return src;
  } catch (ex) {}
};

// ── Record: live transcript ticker ──
// Called via wisperConnectLiveStream's onLine callback in record.html.
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

  // Newest on top; older lines fade but are never removed (session scrollback).
  ticker.insertBefore(line, ticker.firstChild);

  var lines = ticker.querySelectorAll('div[style*="grid-template-columns"]');
  lines.forEach(function(l, i) {
    l.style.opacity = Math.max(0.45, 1 - i * 0.12);
  });
};

// ── Record: "Add marker" flagged line ──
// Called after a successful POST /record/marker. Rose, italic, and
// speaker-less so it can't be mistaken for transcript.
window.wisperTickerAppendMarker = function(elapsedS) {
  var ticker = document.getElementById('live-ticker');
  if (!ticker) return;

  var placeholder = ticker.querySelector('div[style*="font-style"]');
  if (placeholder) placeholder.remove();

  var s = Math.max(0, Math.floor(elapsedS || 0));
  var m = Math.floor(s / 60), ss = s % 60;
  var label = m + ':' + String(ss).padStart(2, '0');

  var line = document.createElement('div');
  line.style.cssText = 'display:grid;grid-template-columns:60px 90px 1fr;gap:14px;padding:6px 0;align-items:baseline;opacity:1';
  line.innerHTML =
    '<span style="font-family:var(--font-mono);font-size:10.5px;color:var(--color-signal-rose)">' + label + '</span>' +
    '<span style="display:flex;align-items:center;gap:7px">' +
      '<span class="dot-rose" style="width:6px;height:6px"></span>' +
      '<span style="font-family:var(--font-serif);font-size:13px;color:var(--color-signal-rose);font-style:italic">Marker</span>' +
    '</span>' +
    '<span style="font-size:13.5px;color:var(--color-paper-faint);font-style:italic">flagged moment</span>';

  ticker.insertBefore(line, ticker.firstChild);

  var lines = ticker.querySelectorAll('div[style*="grid-template-columns"]');
  lines.forEach(function(l, i) {
    l.style.opacity = Math.max(0.45, 1 - i * 0.12);
  });
};

// ── Inline audio excerpt player ──
// Used on the Speakers page and the enrollment wizard.
// Toggles play/pause on a hidden <audio> element; only one clip plays at a time.
// Uses innerHTML (not textContent) so the SVG icon inside the button is preserved.
(function() {
  var _playing = null;

  function _setLabel(btn, label) {
    // Replace the visible text while keeping the SVG icon intact.
    btn.innerHTML = btn.innerHTML.replace(/\bSample\b|\bStop\b/, label);
  }

  window.wisperPlayExcerpt = function(audioId, btn) {
    var audio = document.getElementById(audioId);
    if (!audio) return;

    if (_playing && _playing !== audio) {
      _playing.pause();
      _playing.currentTime = 0;
      var prevBtn = document.querySelector('[data-audio-id="' + _playing.id + '"]');
      if (prevBtn) { delete prevBtn.dataset.playing; _setLabel(prevBtn, 'Sample'); }
      _playing = null;
    }

    if (btn.dataset.playing) {
      audio.pause();
      audio.currentTime = 0;
      delete btn.dataset.playing;
      _setLabel(btn, 'Sample');
      _playing = null;
    } else {
      btn.dataset.playing = '1';
      _setLabel(btn, 'Stop');
      audio.play();
      audio.onended = function() {
        delete btn.dataset.playing;
        _setLabel(btn, 'Sample');
        _playing = null;
      };
      _playing = audio;
    }
  };
})();

// ── File upload feedback ──
document.addEventListener('DOMContentLoaded', function() {
  // Auto-scroll any log terminal
  var terminal = document.getElementById('log-terminal');
  if (terminal) terminal.scrollTop = terminal.scrollHeight;
});

// ── Global recording-status banner ──
// Every page except /record (which has its own toolbar). Polls the JSON
// status endpoint; an EventSource would be reopened on every navigation.
(function() {
  var banner = document.getElementById('global-recording-banner');
  if (!banner) return;
  if (location.pathname === '/record') return;

  var elapsedTimer = null;
  var startTs = null;

  function pad(n) { return String(n).padStart(2, '0'); }
  function escapeHtml(s) {
    var div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
  }
  function tickElapsed() {
    if (!startTs) return;
    var s = Math.floor(Date.now() / 1000 - startTs);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    var el = document.getElementById('global-elapsed-timer');
    if (el) el.textContent = pad(h) + ':' + pad(m) + ':' + pad(ss);
  }

  function render(status) {
    if (!status || !status.active) {
      banner.style.display = 'none';
      banner.innerHTML = '';
      if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
      startTs = null;
      return;
    }

    startTs = status.started_at ? new Date(status.started_at).getTime() / 1000 : null;
    var stopUrl = status.source === 'local' ? '/record/stop-local' : '/record/stop';
    var label = status.name
      ? escapeHtml(status.name)
      : (status.source === 'local' ? 'Local capture' : ('#' + (status.voice_channel_id || '?')));

    banner.innerHTML =
      '<div style="display:flex;align-items:center;gap:18px;padding:10px 24px;' +
      'background:linear-gradient(180deg,#e88b8b10 0%,transparent 100%);' +
      'border-bottom:1px solid var(--color-rule)">' +
        '<div style="display:flex;align-items:center;gap:9px">' +
          '<span class="dot-rose"></span>' +
          '<span style="font-family:var(--font-mono);font-size:10px;color:var(--color-signal-rose);' +
          'letter-spacing:0.14em;font-weight:600">RECORDING</span>' +
        '</div>' +
        '<span id="global-elapsed-timer" style="font-family:var(--font-mono);font-size:13px;color:var(--color-paper)">00:00:00</span>' +
        '<a href="/record" style="margin-right:auto;font-family:var(--font-mono);font-size:11px;color:var(--color-paper-dim)">' +
          label + ' — view →' +
        '</a>' +
        '<form method="post" action="' + stopUrl + '" style="margin:0">' +
          '<button type="submit" class="btn btn-rose btn-sm">Stop recording</button>' +
        '</form>' +
      '</div>';
    banner.style.display = 'block';
    tickElapsed();
    if (!elapsedTimer) elapsedTimer = setInterval(tickElapsed, 1000);
  }

  function poll() {
    fetch('/api/record/status')
      .then(function(r) { return r.json(); })
      .then(render)
      .catch(function() {});
  }

  poll();
  setInterval(poll, 4000);
})();

// ── Sidebar status fallback ──
// htmx polls this via hx-trigger="load, every 5s"; this fallback runs only
// when htmx isn't loaded, so the Device / Jobs cells are never blank.
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

// Transcript playback: highlight the block being spoken, click or deep link to seek.
(function() {
  var audio = document.getElementById('transcript-audio');
  if (!audio) return;
  var blocks = Array.prototype.slice.call(document.querySelectorAll('[data-start]'))
    .map(function(el) { return { el: el, start: parseFloat(el.dataset.start) }; })
    .sort(function(a, b) { return a.start - b.start; });
  var toggle = document.getElementById('follow-toggle');
  var scroller = document.getElementById('player-bar').parentElement;
  var follow = true, current = -1;

  function setToggle() {
    toggle.setAttribute('aria-pressed', follow ? 'true' : 'false');
    toggle.textContent = 'Follow along: ' + (follow ? 'on' : 'off');
  }
  if (blocks.length && toggle) {
    toggle.addEventListener('click', function() { follow = !follow; setToggle(); });
    ['wheel', 'touchmove', 'keydown'].forEach(function(evt) {
      scroller.addEventListener(evt, function() { if (follow) { follow = false; setToggle(); } },
                                { passive: true });
    });
  }

  function indexAt(t) {
    var lo = 0, hi = blocks.length - 1, found = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (blocks[mid].start <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return found;
  }
  audio.addEventListener('timeupdate', function() {
    var i = indexAt(audio.currentTime);
    if (i === current) return;
    if (current >= 0) blocks[current].el.classList.remove('block-playing');
    current = i;
    if (i < 0) return;
    blocks[i].el.classList.add('block-playing');
    if (follow) blocks[i].el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  });

  function seek(t, play) {
    audio.currentTime = t;
    if (play) audio.play();
  }
  blocks.forEach(function(b) {
    b.el.addEventListener('click', function(e) {
      if (e.target.closest('a')) return;
      seek(b.start, true);
    });
  });
  document.querySelectorAll('[data-seek]').forEach(function(btn) {
    btn.addEventListener('click', function() { seek(parseFloat(btn.dataset.seek), true); });
  });
  if (location.hash.indexOf('#b-') === 0) {
    var target = document.getElementById(location.hash.slice(1));
    if (target && target.dataset.start) seek(parseFloat(target.dataset.start), false);
  }
})();
