/* Shared by the Watch tab and the channel pages: video card markup, watched
   state, and the in-page player.

   A video here is the JSON shape built by services/watch.py: {key, videoId,
   title, channel, published, duration, url, thumb, nebula, archived,
   jellyfin, watched, progress}. */
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
  // ``title`` overrides the tooltip, for where the button means something else.
  function watchBtn(v, title) {
    return '<button type="button" class="w-check" data-act="watch" title="' +
      (title || (v.watched ? 'Mark unwatched (W)' : 'Mark watched (W)')) + '">' + CHECK + '</button>';
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
        progressBar(v) +
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
  var resumedAt = 0;   // where the video playing picked up from, if it resumed
  var fellBack = false;   // the archived copy wouldn't play, so this is YouTube's
  var sbBar = null;

  function ensureOverlay() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'w-player-overlay';
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="w-player" role="dialog" aria-modal="true" aria-labelledby="w-player-title">' +
        '<div class="w-player-stage"></div>' +
        '<div class="w-player-sb"></div>' +
        '<div class="w-player-info"></div>' +
      '</div>';
    // A page can give the player a place in its layout for theater mode.
    (document.getElementById('w-player-host') || document.body).appendChild(overlay);
    stage = overlay.querySelector('.w-player-stage');
    sbBar = overlay.querySelector('.w-player-sb');
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
      else if (act === 'restart') play(cur, withOpts({fromStart: true}));
    });
    // Capture phase, so Esc and the arrows reach the player before the
    // page's own shortcuts see them. Everything else is left to the video
    // element (space, f, m…).
    document.addEventListener('keydown', function (e) {
      if (!isOpen()) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); return; }
      if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !(e.metaKey || e.ctrlKey || e.altKey || e.shiftKey)) {
        var t = e.target, qs = document.getElementById('qs-overlay');
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        if (qs && !qs.hidden) return;
        // Also when the <video> has focus, so its own controls don't seek
        // by a different step on top.
        e.preventDefault(); e.stopImmediatePropagation();
        seekBy(e.key === 'ArrowLeft' ? -SEEK_STEP : SEEK_STEP);
      }
    }, true);
  }

  // Phones and touch tablets hand videos to their own apps (YouTube,
  // Jellyfin) via the plain link instead of the overlay. Decided by the
  // device rather than the window's width, so a desktop browser in a narrow
  // window still plays here.
  var handOff = window.matchMedia('(hover: none) and (pointer: coarse)');
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
    var notice = fellBack ? '<div class="w-player-notice">The archived copy wouldn\'t play here, so this is ' +
      'YouTube\'s instead.</div>' : '';
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
    if (resumedAt) actions += pbtn('restart', 'Resumed at ' + dur(resumedAt) + ' · Start over');
    actions +=
      (v.jellyfin ? '<a class="w-plink" href="' + esc(v.url) + '" target="_blank" rel="noopener">Open in Jellyfin ↗</a>' : '') +
      '<a class="w-plink" href="' + esc(youtubeUrl(v)) + '" target="_blank" rel="noopener">Open on YouTube ↗</a>' +
      '<span class="w-pspacer"></span>' + pbtn('close', 'Close', 'Esc');
    info.innerHTML =
      '<div class="w-player-eyebrow">' + eyebrow + '</div>' +
      '<h2 class="w-player-title" id="w-player-title">' + esc(v.title) + '</h2>' +
      '<div class="w-player-sub">' + esc(v.channel) + ' · ' + ago(v) + (d ? ' · ' + d : '') + '</div>' +
      notice +
      '<div class="w-player-actions">' + actions + '</div>';
  }
  // Re-draw the panel and mode after the page's state changes underneath it.
  function refresh() { if (isOpen()) renderInfo(); }

  // ---- resume points ----------------------------------------------------
  // Where each video was left, saved while it plays so the next play picks
  // up there. ``v.progress`` is {pos, dur, at} or null, and follows the same
  // rules as the server (services/watch.py): too near the start or the end
  // and there's nothing to resume.
  var RESUME_MIN = 10, RESUME_DONE = 0.9, SAVE_EVERY_MS = 10000;
  var track = null;   // the video playing: {v, pos(), dur(), started, saved, off}

  function resumable(pos, d) { return pos >= RESUME_MIN && !(d && pos >= d * RESUME_DONE); }
  function resumeAt(v) { return v.progress && resumable(v.progress.pos, v.progress.dur) ? v.progress.pos : 0; }
  // ``pos``/``d`` read the player's position and duration; ``seek`` moves it.
  function startTracking(v, pos, d, seek) {
    var t = {v: v, pos: pos, dur: d, seek: seek, started: false, saved: null, off: false};
    t.timer = setInterval(function () { saveProgress(t); }, SAVE_EVERY_MS);
    track = t;
    return t;
  }
  // Nothing is saved until playback has actually started: before that the
  // position reads 0, which would wipe out the point being resumed from.
  function saveProgress(t, keepalive) {
    if (!t || !t.started || t.off) return;
    var pos, d;
    try { pos = Math.floor(t.pos() || 0); d = Math.round(t.dur() || t.v.duration || 0) || null; } catch (e) { return; }
    if (pos === t.saved) return;
    t.saved = pos;
    // Keep the first ``at`` this session, so "Continue watching" doesn't
    // reshuffle under the video as it plays.
    var at = (t.v.progress && t.v.progress.at) || new Date().toISOString();
    t.v.progress = resumable(pos, d) ? {pos: pos, dur: d, at: at} : null;
    fetch('/watch/progress', {
      method: 'POST', keepalive: !!keepalive,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: t.v.key, position: pos, duration: d})
    }).catch(function () {});
  }
  // Save where the video playing got to, then stop following it — nothing
  // fired later (a removed <video>'s pause event, say) can save over that.
  function stopTracking() {
    stopSponsorBlock();
    if (!track) return;
    clearInterval(track.timer);
    saveProgress(track, true);
    track.started = false;
    track = null;
  }
  window.addEventListener('pagehide', function () { if (track) saveProgress(track, true); });

  // Whether ``v`` is playing and saving its resume point as it goes.
  function tracking(v) { return !!track && track.v === v && !track.off; }
  // Stop (or restart) saving ``v``'s resume point while it plays: once it's
  // been marked done, the next save would only put it back in Continue
  // watching. Does nothing unless ``v`` is the video playing.
  function setTracking(v, on) {
    if (!track || track.v !== v) return;
    track.off = !on;
    track.saved = null;
  }

  // Set or (with null) forget ``v``'s resume point by hand, outside of
  // playback. Resolves on success, rejects on failure.
  function setProgress(v, p) {
    v.progress = p;
    return fetch('/watch/progress', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      // Position 0 is too near the start to resume from, so the server drops it.
      body: JSON.stringify({key: v.key, position: p ? p.pos : 0, duration: p ? p.dur : null})
    }).then(function (r) { if (!r.ok) throw new Error(r.status); });
  }

  // ---- SponsorBlock -------------------------------------------------------
  // Segments for the video playing, per the channel's preference (see
  // services/sponsorblock.py): 'show' marks them under the video and offers
  // a skip button while one plays; 'skip' jumps past each one once, so
  // seeking back into it plays it, and offers to undo.
  var SB_LABEL = {
    sponsor: 'sponsor', selfpromo: 'self-promotion', interaction: 'interaction reminder', intro: 'intro',
    outro: 'outro', preview: 'preview', hook: 'hook', filler: 'filler', music_offtopic: 'non-music section'
  };
  var sb = null;   // {t, mode, segs, drawn, btn, until, onClick}

  function startSponsorBlock(t) {
    // The channel (or playlist) it's shown under picks the mode.
    var url = '/watch/segments/' + encodeURIComponent(t.v.videoId) + '?channel=' + encodeURIComponent(t.v.channelId || '');
    fetch(url, {headers: {'Accept': 'application/json'}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || d.mode === 'off' || !d.segments.length || track !== t) return;
        sb = {t: t, mode: d.mode, segs: d.segments, drawn: false, btn: null, until: 0, onClick: null};
        sb.timer = setInterval(sbTick, 400);
      })
      .catch(function () {});
  }
  function stopSponsorBlock() {
    if (!sb) return;
    clearInterval(sb.timer);
    if (sb.btn) sb.btn.remove();
    sbBar.innerHTML = '';
    sb = null;
  }
  function sbTick() {
    var s = sb, t = s.t, pos, d;
    try { pos = t.pos(); d = t.dur() || t.v.duration; } catch (e) { return; }
    if (!s.drawn && d) {
      s.drawn = true;
      sbBar.innerHTML = s.segs.map(function (g) {
        return '<span class="w-sb-seg w-sb-' + esc(g.category) + '" title="' + esc(SB_LABEL[g.category] || g.category) +
          '" style="left:' + (g.start / d * 100) + '%;width:' + Math.max(0.4, (g.end - g.start) / d * 100) + '%"></span>';
      }).join('');
    }
    if (!t.started || pos == null) return;
    var seg = null;
    for (var i = 0; i < s.segs.length; i++) {
      var g = s.segs[i];
      if (pos >= g.start && pos < g.end - 0.5) { seg = g; break; }
    }
    var label = seg && (SB_LABEL[seg.category] || seg.category);
    if (s.mode === 'skip') {
      if (seg && !seg.skipped) {
        seg.skipped = true;
        t.seek(seg.end);
        var from = seg.start;
        sbShow(s, 'Skipped ' + label + ' · Undo', function () { t.seek(from); });
        s.until = Date.now() + 5000;
      } else if (s.until && Date.now() > s.until) {
        s.until = 0;
        sbShow(s, null);
      }
    } else if (seg) {
      var to = seg.end;
      sbShow(s, 'Skip ' + label + ' ⏭', function () { t.seek(to); });
    } else {
      sbShow(s, null);
    }
  }
  // The button over the video: shown with ``text``, or hidden with null.
  function sbShow(s, text, onClick) {
    if (!s.btn) {
      s.btn = document.createElement('button');
      s.btn.type = 'button';
      s.btn.className = 'w-sb-btn';
      s.btn.addEventListener('click', function () {
        if (s.onClick) s.onClick();
        s.until = 0;
        sbShow(s, null);
      });
      stage.appendChild(s.btn);
    }
    s.btn.hidden = !text;
    if (text) { s.btn.textContent = text; s.onClick = onClick; }
  }

  // ← / →: back or forward SEEK_STEP seconds, as on YouTube. While the
  // YouTube embed has focus its own player takes the keys instead, which
  // does the same.
  var SEEK_STEP = 5;
  var seekFlash = null, seekFlashT = null;
  function seekBy(d) {
    var t = track, pos, len;
    if (!t) return;
    try { pos = t.pos(); len = t.dur() || t.v.duration; } catch (e) { return; }
    if (pos == null) return;   // the YouTube API hasn't attached yet
    var to = Math.max(0, pos + d);
    if (len) to = Math.min(to, Math.max(0, len - 0.5));
    t.seek(to);
    if (!seekFlash) {
      seekFlash = document.createElement('div');
      seekFlash.className = 'w-seek-flash';
    }
    if (seekFlash.parentNode !== stage) stage.appendChild(seekFlash);
    seekFlash.textContent = d < 0 ? '« ' + SEEK_STEP + 's' : SEEK_STEP + 's »';
    seekFlash.classList.toggle('back', d < 0);
    seekFlash.hidden = false;
    clearTimeout(seekFlashT);
    seekFlashT = setTimeout(function () { seekFlash.hidden = true; }, 700);
  }

  // A thin bar along the foot of a thumbnail showing how far in the resume
  // point is.
  function progressBar(v) {
    var p = v.progress, d = p && (p.dur || v.duration);
    if (!d) return '';
    return '<div class="w-prog"><span style="width:' + Math.min(100, Math.round(p.pos / d * 100)) + '%"></span></div>';
  }

  // YouTube's iframe API, loaded the first time a YouTube video plays: the
  // embed can't otherwise be asked where it's up to.
  var ytApi = null;
  function youTubeApi() {
    if (!ytApi) ytApi = new Promise(function (resolve, reject) {
      var prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = function () { if (prev) prev(); resolve(window.YT); };
      var s = document.createElement('script');
      s.src = 'https://www.youtube.com/iframe_api';
      s.onerror = function () { ytApi = null; reject(new Error('iframe API failed to load')); };
      document.head.appendChild(s);
    });
    return ytApi;
  }

  // Play ``v``, from its resume point unless ``o.fromStart``. ``o.source``
  // forces 'file' or 'youtube' (``o.fellBack`` saying why it's YouTube's);
  // ``o.onClose`` runs when the player is
  // dismissed; the rest is described at renderInfo. Playing another video
  // while open replaces the one playing.
  function play(v, o) {
    o = o || {};
    ensureOverlay();
    stopTracking();   // the video being replaced
    var onClose = o.onClose || opts.onClose;
    opts = o;
    opts.onClose = onClose;
    var source = o.source || (v.archived ? 'file' : 'youtube');
    cur = v;
    curSource = source;
    fellBack = source === 'youtube' && !!o.fellBack;
    resumedAt = o.fromStart ? 0 : resumeAt(v);
    var t;

    if (source === 'file') {
      stage.innerHTML = '<video controls autoplay preload="metadata" src="/watch/media/' + encodeURIComponent(v.videoId) + '"></video>';
      var video = stage.querySelector('video');
      video.addEventListener('error', function () { if (track === t) fallBack(v); });
      video.addEventListener('loadedmetadata', function () {
        // A container the browser opens but a video codec it can't decode
        // plays as sound over black, which is no better than an error.
        if (!video.videoWidth) { if (track === t) fallBack(v); return; }
        if (resumedAt) video.currentTime = resumedAt;
      }, {once: true});
      t = startTracking(v, function () { return video.currentTime; }, function () { return video.duration; },
        function (sec) { video.currentTime = sec; });
      video.addEventListener('playing', function () { if (track === t) t.started = true; });
      video.addEventListener('pause', function () { saveProgress(t); });
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
        '?autoplay=1&rel=0&enablejsapi=1&origin=' + encodeURIComponent(location.origin) +
        (resumedAt ? '&start=' + resumedAt : '') + '" title="' + esc(v.title) +
        '" allow="autoplay; encrypted-media; picture-in-picture; fullscreen"' +
        ' allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>';
      var iframe = stage.querySelector('iframe');
      t = startTracking(v, function () { return t.yt && t.yt.getCurrentTime(); }, function () { return t.yt && t.yt.getDuration(); },
        function (sec) { if (t.yt && t.yt.seekTo) t.yt.seekTo(sec, true); });
      // Without the API the video still plays; it just isn't tracked.
      youTubeApi().then(function (YT) {
        if (track !== t) return;   // closed or replaced while the API loaded
        var playing = function (state) { return state === YT.PlayerState.PLAYING; };
        t.yt = new YT.Player(iframe, {events: {
          // The embed autoplays, so it may be playing before the API attaches.
          onReady: function (e) { if (track === t && playing(e.target.getPlayerState())) t.started = true; },
          onStateChange: function (e) {
            if (track !== t) return;
            if (playing(e.data)) t.started = true;
            else if (e.data === YT.PlayerState.PAUSED || e.data === YT.PlayerState.ENDED) saveProgress(t);
          }
        }});
      }).catch(function () {});
    }
    startSponsorBlock(t);
    overlay.hidden = false;
    renderInfo();
  }
  // ``opts`` with ``extra`` on top, to play the same video again differently.
  function withOpts(extra) {
    var o = {};
    Object.keys(opts).forEach(function (k) { o[k] = opts[k]; });
    Object.keys(extra).forEach(function (k) { o[k] = extra[k]; });
    return o;
  }
  // The archived copy won't play here (a format the browser can't handle,
  // or the file's gone): switch to YouTube's, picking up where it got to.
  function fallBack(v) { play(v, withOpts({source: 'youtube', fellBack: true})); }
  function close() {
    if (!overlay) return;
    stopTracking();
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
    badges: badges, watchBtn: watchBtn, thumb: thumb, card: card, progressBar: progressBar, setProgress: setProgress, saveWatched: saveWatched,
    canPlayHere: canPlayHere, play: play, close: close, isOpen: isOpen, refresh: refresh,
    tracking: tracking, setTracking: setTracking
  };
})();
