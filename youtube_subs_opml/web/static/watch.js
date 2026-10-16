/* Shared by the Watch tab and the channel pages: video card markup, watched
   state, and the in-page player.

   A video here is the JSON shape built by services/watch.py: {key, videoId,
   title, channel, published, duration, url, thumb, nebula, archived,
   jellyfin, watched}. */
window.Watch = (function () {
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function hue(s) { var h = 7; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360; return h; }
  function hoursAgo(v) { return Math.max(0, Math.round((Date.now() - Date.parse(v.published)) / 3600000)); }
  function ago(v) {
    var h = hoursAgo(v);
    if (h < 24) return h + 'h ago';
    var d = Math.round(h / 24);
    return d < 60 ? d + 'd ago' : Math.round(d / 30) + 'mo ago';
  }
  function agoShort(v) { return ago(v).replace(' ago', ''); }
  function dur(sec) {
    if (sec == null) return '';
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = String(sec % 60).padStart(2, '0');
    return h ? h + ':' + String(m).padStart(2, '0') + ':' + s : m + ':' + s;
  }

  var CHECK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"></path></svg>';

  function badges(v) {
    return (v.nebula ? '<span class="w-tag">NEB</span>' : '') +
      (v.archived ? '<span class="w-tag w-tag-arch" title="' +
        (v.jellyfin ? 'Archived — opens in Jellyfin' : 'Archived — not in your Jellyfin library yet') + '">Archived</span>' : '');
  }
  function watchBtn(v) {
    return '<button type="button" class="w-check" data-act="watch" title="' +
      (v.watched ? 'Mark unwatched (W)' : 'Mark watched (W)') + '">' + CHECK + '</button>';
  }
  function thumb(v) {
    if (v.thumb) return '<img src="' + esc(v.thumb) + '" alt="" loading="lazy" decoding="async">';
    var H = hue(v.channel);
    return '<div class="w-thumb-ph" style="background:linear-gradient(160deg, oklch(0.58 0.12 ' + H + ') 0%, oklch(0.36 0.08 ' +
      (H + 35) % 360 + ') 60%, oklch(0.24 0.04 ' + (H + 60) % 360 + ') 100%)"></div>';
  }
  // A grid/shelf card. ``k`` is the data-k the page uses to find it again;
  // ``opts.hideChannel`` drops the channel name (on that channel's own page).
  function card(v, k, opts) {
    var showChannel = !(opts && opts.hideChannel);
    var d = dur(v.duration);
    return '<a class="w-card' + (v.watched ? ' watched' : '') + '" data-k="' + esc(k) +
      '" href="' + esc(v.url) + '" target="_blank" rel="noopener">' +
      '<div class="w-thumb">' + thumb(v) +
        '<div class="w-thumb-tl">' + badges(v) + '</div>' +
        '<div class="w-thumb-bl">' + (d ? '<span class="w-dur">' + d + '</span>' : '') +
          (v.watched ? '<span class="w-watched-tag">Watched</span>' : '') + '</div>' +
      '</div>' +
      '<div class="w-card-body"><div class="w-card-text">' +
        '<div class="w-title">' + esc(v.title) + '</div>' +
        '<div class="w-meta">' + (v.watched ? '' : '<span class="dot"></span>') +
          (showChannel ? '<span class="w-meta-ch">' + esc(v.channel) + '</span><span>·</span>' : '') +
          '<span class="nowrap">' + ago(v) + '</span></div>' +
      '</div>' + watchBtn(v) + '</div></a>';
  }

  // Save watched state; resolves on success, rejects on failure.
  function saveWatched(keys, watched) {
    return fetch('/watch/watched', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({keys: keys, watched: watched})
    }).then(function (r) { if (!r.ok) throw new Error(r.status); });
  }

  // ---- in-page player ---------------------------------------------------
  // Archived videos play from this app's copy of the file; everything else on
  // YouTube plays in YouTube's embed. Nebula has no embeddable player, so the
  // caller opens those in a new tab instead (see canPlayHere).
  var overlay = null, stage = null, onClose = null;

  function ensureOverlay() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'w-player-overlay';
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="w-player" role="dialog" aria-modal="true" aria-labelledby="w-player-title">' +
        '<div class="w-player-head">' +
          '<div class="w-player-meta"><div class="w-player-title" id="w-player-title"></div>' +
            '<div class="w-player-sub"></div></div>' +
          '<div class="w-player-links"></div>' +
          '<button type="button" class="toast-x" data-act="close" title="Close (Esc)">&times;</button>' +
        '</div>' +
        '<div class="w-player-stage"></div>' +
      '</div>';
    document.body.appendChild(overlay);
    stage = overlay.querySelector('.w-player-stage');
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) close(); });
    overlay.querySelector('[data-act="close"]').addEventListener('click', close);
    // Capture phase, so Esc closes the player before the page's own shortcuts
    // see it. Everything else is left to the video element (space, f, m…).
    document.addEventListener('keydown', function (e) {
      if (isOpen() && e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); }
    }, true);
  }

  // Phones and touch tablets hand videos to their own apps (YouTube,
  // Jellyfin) via the plain link instead of the overlay. Matches the 860px
  // breakpoint where the layout goes single-column.
  var handOff = window.matchMedia('(max-width: 860px), (hover: none) and (pointer: coarse)');
  function canPlayHere(v) { return !handOff.matches && !v.nebula && !!v.videoId; }
  function youtubeUrl(v) { return 'https://www.youtube.com/watch?v=' + encodeURIComponent(v.videoId); }
  function isOpen() { return !!overlay && !overlay.hidden; }

  // Play ``v`` in the overlay. ``opts.source`` forces 'file' or 'youtube';
  // ``opts.onClose`` runs when the player is dismissed.
  function play(v, opts) {
    opts = opts || {};
    ensureOverlay();
    var source = opts.source || (v.archived ? 'file' : 'youtube');
    onClose = opts.onClose || onClose;
    overlay.querySelector('.w-player-title').textContent = v.title;
    overlay.querySelector('.w-player-sub').textContent =
      v.channel + ' · ' + ago(v) + ' · ' + (source === 'file' ? 'Archived copy' : 'YouTube');
    overlay.querySelector('.w-player-links').innerHTML =
      (v.jellyfin ? '<a class="btn btn-sm btn-ghost" href="' + esc(v.url) + '" target="_blank" rel="noopener">Open in Jellyfin ↗</a>' : '') +
      '<a class="btn btn-sm btn-ghost" href="' + esc(youtubeUrl(v)) + '" target="_blank" rel="noopener">Open on YouTube ↗</a>';

    if (source === 'file') {
      stage.innerHTML = '<video controls autoplay preload="metadata" src="/watch/media/' + encodeURIComponent(v.videoId) + '"></video>';
      var video = stage.querySelector('video');
      video.addEventListener('error', function () { playError(v); });
      if (v.jellyfin) {
        // Tell Jellyfin once it's essentially been watched, so it stops
        // offering the episode as unplayed.
        var reported = false;
        video.addEventListener('timeupdate', function () {
          if (reported || !video.duration || video.currentTime < video.duration * 0.9) return;
          reported = true;
          fetch('/watch/played/' + encodeURIComponent(v.videoId), {method: 'POST'});
        });
      }
    } else {
      stage.innerHTML = '<iframe src="https://www.youtube-nocookie.com/embed/' + encodeURIComponent(v.videoId) +
        '?autoplay=1&rel=0" title="' + esc(v.title) + '" allow="autoplay; encrypted-media; picture-in-picture; fullscreen"' +
        ' allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>';
    }
    overlay.hidden = false;
  }
  function playError(v) {
    stage.innerHTML = '<div class="w-player-error"><div class="eyebrow">Can\'t play the archived copy in this browser</div>' +
      '<p>The file is Matroska (.mkv), which this browser may not support.</p><div class="w-player-error-actions">' +
      '<button type="button" class="btn btn-primary" data-act="play-youtube">Play the YouTube version</button>' +
      (v.jellyfin ? '<a class="btn" href="' + esc(v.url) + '" target="_blank" rel="noopener">Open in Jellyfin ↗</a>' : '') +
      '</div></div>';
    stage.querySelector('[data-act="play-youtube"]').addEventListener('click', function () { play(v, {source: 'youtube'}); });
  }
  function close() {
    if (!overlay) return;
    stage.innerHTML = '';   // removing the element is what stops playback
    overlay.hidden = true;
    var cb = onClose;
    onClose = null;
    if (cb) cb();
  }

  return {
    esc: esc, hue: hue, ago: ago, agoShort: agoShort, dur: dur,
    badges: badges, watchBtn: watchBtn, card: card, saveWatched: saveWatched,
    canPlayHere: canPlayHere, play: play, close: close, isOpen: isOpen
  };
})();
