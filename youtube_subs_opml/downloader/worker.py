"""Downloader worker: a separate container polling Postgres as its queue.

Why not pub/sub: Redis pub/sub is fire-and-forget. A message published while
this container is restarting is simply gone, and nothing in the system knows a
video was missed. Redis Streams with consumer groups would fix that, but adds a
second stateful service to get behaviour Postgres already provides.

Here the state lives in a table. The worker claims a row, and if it dies
mid-download the row is still ``downloading`` on the next pass and gets reaped.
Restarts are a non-event.

Why not APScheduler in the web process: downloads are long-running, spawn
ffmpeg, and can hang. ``BackgroundScheduler`` runs threads inside uvicorn,
which is fine for a 6-hour API sync and bad for this. A separate container also
isolates the yt-dlp + ffmpeg image bulk and lets yt-dlp be updated
independently — which matters, because YouTube breaks extraction every few
months.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..web.config import get_settings
from ..web.db import get_session_factory
from ..web.models import Channel, Download, DownloadLink, Video, VideoShort
from ..web.services.archive import (
    channel_intents,
    prune_candidates,
    user_retained_video_ids,
)
from ..web.services.prefs import is_within_duration_limit
from ..web.services.shorts import classify_videos
from . import naming, ytdlp

logger = logging.getLogger(__name__)

_IDLE_SLEEP = 30.0
# Rows stuck in 'downloading' longer than this are assumed orphaned by a crash.
_STALE_CLAIM = timedelta(hours=6)
_MAX_ATTEMPTS = 5

_shutdown = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _shutdown
    logger.info("Received signal %s, finishing current job then exiting", signum)
    _shutdown = True


def _backoff(attempts: int) -> datetime:
    """Exponential backoff, capped at 6 hours."""
    delay = min(2 ** attempts * 60, 6 * 3600)
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


# yt-dlp probe errors that will never succeed from this IP/account. Skipping
# them on the first failure avoids burning five back-off'd retries over hours on
# something permanent. Deliberately conservative: "Sign in to confirm you're not
# a bot" and other throttling ARE transient and must keep retrying, so they are
# NOT listed here.
_TERMINAL_PROBE_ERRORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("geo_blocked", (
        "available in your country",       # "not made this video available in your country"
        "blocked it in your country",
    )),
    ("members_only", (
        "members-only",
        "available to this channel's members",
        "join this channel",
    )),
    ("unavailable", (
        "private video",
        "removed by the uploader",
        "account associated with this video has been terminated",
        "video is no longer available",
        "confirm your age",
        "age-restricted",
        "inappropriate for some users",
    )),
)


def _terminal_skip_reason(error_text: str) -> str | None:
    """A skip_reason if the probe error is permanent, else None (keep retrying)."""
    low = error_text.lower()
    for reason, needles in _TERMINAL_PROBE_ERRORS:
        if any(needle in low for needle in needles):
            return reason
    return None


def claim_one(db: Session) -> Download | None:
    """Atomically claim the oldest eligible pending row.

    ``FOR UPDATE SKIP LOCKED`` is what makes this safe to run in more than one
    container without a broker. Postgres-only; local SQLite development runs
    the loop single-threaded and the clause is dropped.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Download)
        .where(
            Download.status == "pending",
            (Download.next_attempt_at.is_(None)) | (Download.next_attempt_at <= now),
        )
        .order_by(Download.created_at)
        .limit(1)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    row.status = "downloading"
    row.attempts += 1
    db.commit()
    return row


def reap_stale(db: Session) -> int:
    """Return crashed-mid-download rows to pending."""
    cutoff = datetime.now(timezone.utc) - _STALE_CLAIM
    stale = db.execute(
        select(Download).where(
            Download.status == "downloading", Download.created_at < cutoff
        )
    ).scalars().all()
    for row in stale:
        row.status = "pending"
        row.last_error = "reclaimed after stale claim"
    if stale:
        db.commit()
        logger.warning("Reaped %d stale downloads", len(stale))
    return len(stale)


def process(row: Download, db: Session) -> None:
    """Probe, duration-check, download, write NFO, record result."""
    settings = get_settings()
    video = db.get(Video, row.video_id)
    if video is None:
        row.status = "skipped"
        row.skip_reason = "unavailable"
        db.commit()
        return

    channel = db.get(Channel, video.channel_id)
    channel_title = channel.title if channel else video.channel_id

    # --- Probe first: never fetch media for something we'll reject ----------
    try:
        meta = ytdlp.probe(row.video_id)
    except ytdlp.ProbeError as exc:
        msg = str(exc)
        row.last_error = msg[:2000]
        terminal = _terminal_skip_reason(msg)
        if terminal is not None:
            # Permanent (geo-block, private, removed, members-only, age-gated):
            # don't waste retries — it will never succeed from here.
            logger.info("Skipping %s permanently (%s)", row.video_id, terminal)
            row.status = "skipped"
            row.skip_reason = terminal
        elif row.attempts >= _MAX_ATTEMPTS:
            logger.warning("Giving up on %s after %d attempts: %s",
                           row.video_id, row.attempts, exc)
            row.status = "skipped"
            row.skip_reason = "unavailable"
        else:
            logger.warning("Probe failed for %s (attempt %d): %s",
                           row.video_id, row.attempts, exc)
            row.status = "pending"
            row.next_attempt_at = _backoff(row.attempts)
        db.commit()
        return

    if meta.duration_seconds is not None:
        video.duration_seconds = meta.duration_seconds

    intent = channel_intents(db).get(video.channel_id)
    if intent is None or not intent.download:
        row.status = "skipped"
        row.skip_reason = "no_subscribers"
        db.commit()
        return

    if not is_within_duration_limit(
        video.duration_seconds, intent.max_duration_seconds
    ):
        logger.info(
            "Skipping %s: %ss exceeds cap of %ss",
            row.video_id,
            video.duration_seconds,
            intent.max_duration_seconds,
        )
        row.status = "skipped"
        row.skip_reason = "too_long"
        db.commit()
        return

    # No subscriber who archives this channel wants its Shorts: probe (result is
    # cached, so per-user retention/library reconcile can read it) and skip if it
    # is one. The probe lives here rather than in the poller so it stays on the
    # rate-limited downloader path, one video at a time.
    if not intent.include_shorts and classify_videos([row.video_id], db).get(row.video_id):
        logger.info("Skipping %s: Short excluded for its channel", row.video_id)
        row.status = "skipped"
        row.skip_reason = "short"
        db.commit()
        return

    # --- Download ----------------------------------------------------------
    episode_number = naming.next_episode_number(
        db, video.channel_id, video.published_at.year if video.published_at else 1970
    )
    basename = naming.episode_basename(
        channel_title, video.published_at, episode_number, video.title or meta.title
    )
    target_dir = naming.canonical_episode_dir(
        settings.media_root, channel_title, video.published_at
    )
    target = target_dir / basename

    try:
        produced = ytdlp.download(
            row.video_id,
            target,
            video_format=settings.ytdlp_format,
            sleep_interval=settings.ytdlp_sleep_interval,
            max_retries=settings.ytdlp_max_retries,
        )
    except ytdlp.DownloadError as exc:
        logger.warning("Download failed for %s: %s", row.video_id, exc)
        row.last_error = str(exc)
        if row.attempts >= _MAX_ATTEMPTS:
            row.status = "failed"
        else:
            row.status = "pending"
            row.next_attempt_at = _backoff(row.attempts)
        db.commit()
        return

    nfo = naming.build_nfo(
        title=video.title or meta.title,
        channel_title=channel_title,
        published_at=video.published_at,
        episode_number=episode_number,
        description=meta.description,
        video_id=row.video_id,
    )
    (target_dir / f"{basename}.nfo").write_bytes(nfo)

    # --- Optional audio extraction for podcast feeds ------------------------
    if intent.generate_podcast:
        try:
            audio = ytdlp.extract_audio(
                row.video_id,
                target_dir / f"{basename}",
                sleep_interval=settings.ytdlp_sleep_interval,
            )
            row.audio_path = str(audio)
            row.audio_size_bytes = audio.stat().st_size
        except ytdlp.DownloadError as exc:
            # Non-fatal: the video succeeded, the podcast item just won't appear.
            logger.warning("Audio extraction failed for %s: %s", row.video_id, exc)

    row.file_path = str(produced)
    row.file_size_bytes = produced.stat().st_size
    row.status = "complete"
    row.completed_at = datetime.now(timezone.utc)
    row.last_error = None
    db.commit()
    logger.info("Downloaded %s -> %s", row.video_id, produced)


def _unlink_all(mkv_path: Path) -> None:
    """Remove an episode's .mkv and its sidecars, then any Season/Channel dirs
    the removal has emptied, ignoring already-gone files.

    Without the directory cleanup a fully-pruned channel leaves an empty
    ``{Channel}/Season {YYYY}/`` tree behind, which Jellyfin keeps showing as an
    empty series. The climb is bounded to those two levels — the layout is
    ``{root}/{Channel}/Season {YYYY}/…`` — so it can never reach the library or
    canonical root. ``rmdir`` only removes an empty dir, so a season still
    holding other episodes (or a channel with other seasons) is left untouched.
    """
    for p in naming.episode_files(mkv_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    season_dir = mkv_path.parent
    channel_dir = season_dir.parent
    for d in (season_dir, channel_dir):
        # Art-aware: a dir left holding only poster/backdrop sidecars is
        # logically empty and should go, so a fully-pruned channel doesn't
        # linger as an empty series in Jellyfin.
        if not naming.rmdir_if_stripped(d):
            break  # still holds episodes (or couldn't be removed) — stop climbing


def reconcile_links(db: Session, media_root: str) -> tuple[int, int]:
    """Make per-user hardlinks match each user's retained set. Idempotent.

    Each subscriber's library is a subtree of hardlinks into the canonical
    files. This reconciles that subtree against ``user_retained_video_ids``:
    videos a user gained (new subscription, or a fresh download) get linked in;
    videos they no longer retain get unlinked. Runs after every download and on
    each idle pass, so subscription changes take effect without a re-download.

    Returns ``(created, removed)`` link counts.
    """
    desired = user_retained_video_ids(db)

    # Only completed downloads with a real canonical file can be linked.
    completed = {
        d.video_id: d
        for d in db.execute(
            select(Download).where(Download.status == "complete")
        ).scalars().all()
        if d.file_path
    }
    videos = {v.video_id: v for v in db.execute(select(Video)).scalars().all()}
    channels = {c.channel_id: c for c in db.execute(select(Channel)).scalars().all()}
    existing = {
        (link.user_id, link.video_id): link
        for link in db.execute(select(DownloadLink)).scalars().all()
    }

    wanted: set[tuple[int, str]] = {
        (user_id, vid)
        for user_id, vids in desired.items()
        for vid in vids
        if vid in completed
    }

    created = removed = 0

    # Add missing links.
    for user_id, vid in wanted - set(existing):
        download = completed[vid]
        video = videos.get(vid)
        channel = channels.get(video.channel_id) if video else None
        channel_title = channel.title if channel else (video.channel_id if video else vid)
        src_mkv = Path(download.file_path)
        if not src_mkv.exists():
            logger.warning("Canonical file missing, skipping link: %s", src_mkv)
            continue
        user_dir = naming.user_episode_dir(
            media_root, user_id, channel_title, video.published_at if video else None
        )
        try:
            for src in naming.episode_files(src_mkv):
                naming.hardlink(src, user_dir / src.name)
        except OSError as exc:
            logger.warning("Hardlink failed for user %s / %s: %s", user_id, vid, exc)
            continue
        db.add(
            DownloadLink(
                user_id=user_id,
                video_id=vid,
                link_path=str(user_dir / src_mkv.name),
                linked_at=datetime.now(timezone.utc),
            )
        )
        created += 1

    # Remove links no user wants any more.
    for key in set(existing) - wanted:
        link = existing[key]
        if link.link_path:
            _unlink_all(Path(link.link_path))
        db.delete(link)
        removed += 1

    if created or removed:
        db.commit()
        logger.info("Reconciled links: +%d, -%d", created, removed)
    return created, removed


def run_prune(db: Session) -> int:
    """Delete canonical files no user retains, freeing the last hardlink.

    Per-user hardlinks for these videos are already gone (reconcile removes a
    link the moment no user wants it), so unlinking the canonical file drops the
    inode's last reference and frees the disk. The ``Download`` row is deleted;
    ``download_links`` cascades. Jellyfin playlist/library removal is Phase 3.

    Returns the number of pruned downloads.
    """
    candidates = prune_candidates(db)
    pruned = 0
    for download in candidates:
        if download.file_path:
            _unlink_all(Path(download.file_path))
        db.delete(download)
        pruned += 1
    if pruned:
        db.commit()
        logger.info("Pruned %d downloads", pruned)
    return pruned


def backfill_audio(db: Session, settings) -> int:
    """Extract audio for completed downloads that should have a podcast but don't.

    So enabling ``generate_podcast`` on an already-archived channel produces
    audio without re-downloading the video: on each idle pass we look for
    ``complete`` downloads whose resolved intent now wants a podcast but which
    have no ``audio_path`` yet, and extract audio from YouTube for them. A
    failure (e.g. throttling) is non-fatal and simply retried on a later pass,
    since ``audio_path`` stays NULL.

    Returns the number of downloads given audio this pass.
    """
    intents = channel_intents(db)
    rows = db.execute(
        select(Download).where(
            Download.status == "complete",
            Download.audio_path.is_(None),
            Download.file_path.is_not(None),
        )
    ).scalars().all()

    done = 0
    for download in rows:
        video = db.get(Video, download.video_id)
        if video is None:
            continue
        intent = intents.get(video.channel_id)
        if intent is None or not intent.generate_podcast:
            continue
        output = Path(download.file_path).with_suffix("")
        try:
            audio = ytdlp.extract_audio(
                download.video_id,
                output,
                sleep_interval=settings.ytdlp_sleep_interval,
            )
        except ytdlp.DownloadError as exc:
            logger.warning("Audio backfill failed for %s: %s", download.video_id, exc)
            continue
        download.audio_path = str(audio)
        download.audio_size_bytes = audio.stat().st_size
        db.commit()
        done += 1
    if done:
        logger.info("Backfilled audio for %d downloads", done)
    return done


def backfill_shorts(db: Session, *, limit: int = 15) -> int:
    """Classify already-archived videos on Shorts-excluded channels for pruning.

    Shorts filtering only started gating the download path recently, so channels
    that exclude Shorts may already hold some on disk. This probes a bounded
    batch of their still-unclassified completed downloads each idle pass (each
    video probed at most once — ``classify_videos`` caches the verdict), so
    ``run_prune`` running right after can drop the ones found to be Shorts.

    Kept small per pass so the ``/shorts`` probes stay gentle; the rest are
    picked up on later passes. Returns the number newly identified as Shorts.
    """
    intents = channel_intents(db)
    excluded = {
        cid for cid, intent in intents.items()
        if intent.download and not intent.include_shorts
    }
    if not excluded:
        return 0

    classified = set(db.execute(select(VideoShort.video_id)).scalars().all())
    candidates = db.execute(
        select(Download.video_id)
        .join(Video, Video.video_id == Download.video_id)
        .where(Download.status == "complete", Video.channel_id.in_(excluded))
    ).scalars().all()
    todo = [v for v in candidates if v not in classified][:limit]
    if not todo:
        return 0

    verdicts = classify_videos(todo, db)
    found = sum(1 for v in todo if verdicts.get(v))
    if found:
        logger.info("Backfill classified %d archived Shorts for pruning", found)
    return found


# YouTube serves avatars/banners more reliably to a browser-like UA, same as
# the poller's feed fetches.
_ART_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _fetch_image(url: str, dest: Path) -> bool:
    """Download ``url`` to ``dest`` atomically. Returns success.

    Non-fatal on failure — artwork is a nicety, and a later idle pass retries
    since the destination file stays absent.
    """
    try:
        with httpx.Client(
            timeout=30.0, follow_redirects=True, headers={"User-Agent": _ART_UA}
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.content
    except httpx.HTTPError as exc:
        logger.warning("Artwork fetch failed for %s: %s", url, exc)
        return False
    if not data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True


def backfill_channel_art(db: Session, *, limit: int = 5) -> int:
    """Fetch avatar/banner URLs for archived channels that lack them.

    Only channels with at least one completed download are probed, so we never
    hit YouTube for a channel we don't archive. Bounded per pass to keep these
    extra requests gentle. A successful probe with no images still records an
    empty ``thumbnail_url`` sentinel so the channel isn't re-probed forever;
    genuine failures leave it NULL to retry later.

    Returns the number of channels newly given art this pass.
    """
    archived = (
        select(Video.channel_id)
        .join(Download, Download.video_id == Video.video_id)
        .where(Download.status == "complete")
        .distinct()
    )
    channels = db.execute(
        select(Channel).where(
            Channel.channel_id.in_(archived),
            Channel.thumbnail_url.is_(None),
        )
    ).scalars().all()[:limit]

    done = 0
    for channel in channels:
        try:
            art = ytdlp.probe_channel(channel.channel_id)
        except ytdlp.ProbeError as exc:
            logger.warning("Channel art probe failed for %s: %s",
                           channel.channel_id, exc)
            continue
        # "" is the "probed, none found" sentinel; a real URL is truthy.
        channel.thumbnail_url = art.avatar_url or ""
        channel.banner_url = art.banner_url
        db.commit()
        done += 1
    if done:
        logger.info("Fetched channel art for %d channels", done)
    return done


def _ensure_art_link(src: Path, dst: Path) -> bool:
    """Hardlink ``src`` to ``dst`` if ``src`` exists and ``dst`` doesn't."""
    if src.exists() and not dst.exists():
        naming.hardlink(src, dst)
        return True
    return False


def sync_library_art(db: Session, media_root: str) -> int:
    """Materialise channel/season posters into each user's library. Idempotent.

    Two steps: (1) fetch each archived channel's avatar/banner into a single
    canonical ``poster.jpg``/``backdrop.jpg`` under ``.canonical/{Channel}``,
    then (2) hardlink those into every subscriber's ``{Channel}/`` (series art)
    and ``{Channel}/Season {YYYY}/`` (season art) folders, sharing one inode the
    same way episodes do. Season posters reuse the channel avatar so seasons
    aren't blank tiles. Returns the number of art hardlinks created this pass.
    """
    channels = {c.channel_id: c for c in db.execute(select(Channel)).scalars().all()}
    videos = {v.video_id: v for v in db.execute(select(Video)).scalars().all()}
    links = [
        link
        for link in db.execute(select(DownloadLink)).scalars().all()
        if link.link_path
    ]

    def channel_for(video_id: str) -> Channel | None:
        video = videos.get(video_id)
        channel = channels.get(video.channel_id) if video else None
        # A blank thumbnail_url means "probed, no art" — nothing to place.
        return channel if channel and channel.thumbnail_url else None

    # Step 1: ensure the canonical art files exist for every linked channel.
    for cid in {
        video.channel_id
        for link in links
        if (video := videos.get(link.video_id))
    }:
        channel = channels.get(cid)
        if channel is None or not channel.thumbnail_url:
            continue
        cdir = naming.canonical_channel_dir(media_root, channel.title)
        poster = cdir / naming.POSTER_NAME
        if not poster.exists():
            _fetch_image(channel.thumbnail_url, poster)
        if channel.banner_url:
            backdrop = cdir / naming.BACKDROP_NAME
            if not backdrop.exists():
                _fetch_image(channel.banner_url, backdrop)

    # Step 2: hardlink canonical art into each user's channel + season dirs.
    created = 0
    for link in links:
        channel = channel_for(link.video_id)
        if channel is None:
            continue
        cdir = naming.canonical_channel_dir(media_root, channel.title)
        canon_poster = cdir / naming.POSTER_NAME
        canon_backdrop = cdir / naming.BACKDROP_NAME
        season_dir = Path(link.link_path).parent
        user_channel_dir = season_dir.parent
        try:
            created += _ensure_art_link(canon_poster, user_channel_dir / naming.POSTER_NAME)
            created += _ensure_art_link(canon_poster, season_dir / naming.POSTER_NAME)
            created += _ensure_art_link(
                canon_backdrop, user_channel_dir / naming.BACKDROP_NAME
            )
        except OSError as exc:
            logger.warning("Art hardlink failed for %s: %s", link.link_path, exc)
    if created:
        logger.info("Linked %d library art files", created)
    return created


def run_forever() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    session_factory = get_session_factory()
    media_root = settings.media_root
    logger.info("Downloader worker started (media_root=%s)", media_root)

    # Edge-triggered so we log the transition once, not every idle tick.
    media_ready_last: bool | None = None

    while not _shutdown:
        ready = naming.media_is_ready(
            media_root, allow_unmounted=settings.allow_unmounted_media
        )
        if ready != media_ready_last:
            if ready:
                logger.info("media_root %s is ready; downloads enabled", media_root)
            else:
                logger.warning(
                    "media_root %s is not a writable mount — downloads paused "
                    "until storage is attached. Pending items will wait.",
                    media_root,
                )
            media_ready_last = ready
        if not ready:
            # Worker stays alive; it just doesn't claim or download anything, so
            # nothing lands in the container's ephemeral filesystem.
            time.sleep(_IDLE_SLEEP)
            continue

        db = session_factory()
        try:
            reap_stale(db)
            row = claim_one(db)
            if row is None:
                # Idle: classify any archived Shorts on channels that now
                # exclude them, reconcile per-user libraries against current
                # subscriptions, backfill podcast audio for channels newly
                # opted in, and prune anything no user retains (including those
                # freshly-identified Shorts).
                backfill_shorts(db)
                reconcile_links(db, media_root)
                backfill_audio(db, settings)
                # Give channels their avatars/banners, then materialise
                # series/season posters into each user's library so Jellyfin
                # views don't blend together.
                backfill_channel_art(db)
                sync_library_art(db, media_root)
                run_prune(db)
                db.close()
                time.sleep(_IDLE_SLEEP)
                continue
            process(row, db)
            # Link the freshly downloaded file into its subscribers' libraries
            # immediately, rather than waiting for the next idle pass.
            reconcile_links(db, media_root)
        except Exception:
            logger.exception("Worker iteration failed")
            db.rollback()
            time.sleep(_IDLE_SLEEP)
        finally:
            db.close()

    logger.info("Downloader worker stopped")


if __name__ == "__main__":
    run_forever()
