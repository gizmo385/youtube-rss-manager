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
  //
  // The player is a modal overlay, or — when the page puts it in theater mode
  // (see refresh) — a pane in the page's own layout. Both are the same element
  // restyled, never moved, since moving an iframe reloads it.
  var overlay = null, stage = null, info = null, cur = null, curSource = null, opts = {};

  function ensureOverlay() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'w-player-overlay';
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="w-player" role="dialog" aria-modal="true" aria-labelledby="w-player-title">' +
        '<div class="w-player-stage"></div>' +
        '<div class="w-player-info"></div>' +
      '</div>';
    // A page can give the player a place in its layout for theater mode.
    (document.getElementById('w-player-host') || document.body).appendChild(overlay);
    stage = overlay.querySelector('.w-player-stage');
    info = overlay.querySelector('.w-player-info');
    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay && !isTheater()) close(); });
    info.addEventListener('click', function (e) {
      var b = e.target.closest('[data-act]');
      if (!b) return;
      var act = b.dataset.act;
      if (act === 'close') close();
      else if (act === 'prev' && opts.onPrev) opts.onPrev();
      else if (act === 'next' && opts.onNext) opts.onNext();
      else if (act === 'watch' && opts.onWatch) opts.onWatch();
      else if (act === 'theater' && opts.onTheater) opts.onTheater();
    });
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
  function isTheater() { return !!overlay && overlay.classList.contains('theater'); }

  function pbtn(act, label, key, extra) {
    return '<button type="button" class="w-pbtn' + (extra || '') + '" data-act="' + act + '">' + label +
      (key ? '<kbd>' + key + '</kbd>' : '') + '</button>';
  }
  // The panel under the video. What the page passes in ``opts`` decides
  // which controls show: ``info()`` returns {cat, pos, watched, hasPrev,
  // hasNext, theater}; onPrev/onNext/onWatch/onTheater wire up the buttons.
  function renderInfo() {
    var v = cur, i = opts.info ? opts.info() : {};
    overlay.classList.toggle('theater', !!i.theater);
    overlay.querySelector('.w-player').setAttribute('aria-modal', i.theater ? 'false' : 'true');
    var source = curSource === 'file' ? (v.jellyfin ? 'Jellyfin' : 'Archived copy') : 'YouTube';
    var eyebrow = (i.cat ? '<span class="w-player-cat">' + esc(i.cat) + '</span>' : '') +
      (i.pos ? '<span>' + esc(i.pos) + '</span>' : '') +
      '<span class="w-tag">' + esc(source) + '</span>';
    var d = dur(v.duration);
    var actions = '';
    if (opts.onPrev || opts.onNext) {
      actions += '<div class="w-pbtn-pair">' +
        pbtn('prev', '← Prev', 'K', i.hasPrev ? '' : ' dim') +
        pbtn('next', 'Next →', 'J', i.hasNext ? '' : ' dim') + '</div>';
    }
    if (opts.onWatch) {
      actions += pbtn('watch', CHECK + (i.watched ? 'Watched' : 'Mark watched'), 'W', i.watched ? ' on' : '');
    }
    if (opts.onTheater) actions += pbtn('theater', i.theater ? 'Exit theater' : 'Theater', 'T');
    actions +=
      (v.jellyfin ? '<a class="w-plink" href="' + esc(v.url) + '" target="_blank" rel="noopener">Open in Jellyfin ↗</a>' : '') +
      '<a class="w-plink" href="' + esc(youtubeUrl(v)) + '" target="_blank" rel="noopener">Open on YouTube ↗</a>' +
      '<span class="w-pspacer"></span>' + pbtn('close', 'Close', 'Esc');
    info.innerHTML =
      '<div class="w-player-eyebrow">' + eyebrow + '</div>' +
      '<h2 class="w-player-title" id="w-player-title">' + esc(v.title) + '</h2>' +
      '<div class="w-player-sub">' + esc(v.channel) + ' · ' + ago(v) + (d ? ' · ' + d : '') + '</div>' +
      '<div class="w-player-actions">' + actions + '</div>';
  }
  // Re-draw the panel and mode after the page's state changes underneath it.
  function refresh() { if (isOpen()) renderInfo(); }

  // Play ``v``. ``o.source`` forces 'file' or 'youtube'; ``o.onClose`` runs
  // when the player is dismissed; the rest is described at renderInfo.
  // Playing another video while open replaces the one playing.
  function play(v, o) {
    o = o || {};
    ensureOverlay();
    var onClose = o.onClose || opts.onClose;
    opts = o;
    opts.onClose = onClose;
    var source = o.source || (v.archived ? 'file' : 'youtube');
    cur = v;
    curSource = source;

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
    renderInfo();
  }
  function playError(v) {
    stage.innerHTML = '<div class="w-player-error"><div class="eyebrow">Can\'t play the archived copy in this browser</div>' +
      '<p>The file is Matroska (.mkv), which this browser may not support.</p><div class="w-player-error-actions">' +
      '<button type="button" class="btn btn-primary" data-act="play-youtube">Play the YouTube version</button>' +
      (v.jellyfin ? '<a class="btn" href="' + esc(v.url) + '" target="_blank" rel="noopener">Open in Jellyfin ↗</a>' : '') +
      '</div></div>';
    stage.querySelector('[data-act="play-youtube"]').addEventListener('click', function () {
      var o = {}; Object.keys(opts).forEach(function (k) { o[k] = opts[k]; });
      o.source = 'youtube';
      play(v, o);
    });
  }
  function close() {
    if (!overlay) return;
    stage.innerHTML = '';   // removing the element is what stops playback
    overlay.hidden = true;
    overlay.classList.remove('theater');
    var cb = opts.onClose;
    opts = {};
    cur = null;
    if (cb) cb();
  }

  return {
    esc: esc, hue: hue, ago: ago, agoShort: agoShort, dur: dur,
    badges: badges, watchBtn: watchBtn, thumb: thumb, card: card, saveWatched: saveWatched,
    canPlayHere: canPlayHere, play: play, close: close, isOpen: isOpen, refresh: refresh
  };
})();
