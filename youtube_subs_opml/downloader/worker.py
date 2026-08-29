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

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..web.config import get_settings
from ..web.db import get_session_factory
from ..web.models import Channel, Download, DownloadLink, Video
from ..web.services.archive import (
    channel_intents,
    prune_candidates,
    user_retained_video_ids,
)
from ..web.services.prefs import is_within_duration_limit
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
        logger.warning("Probe failed for %s: %s", row.video_id, exc)
        row.last_error = str(exc)
        if row.attempts >= _MAX_ATTEMPTS:
            row.status = "skipped"
            row.skip_reason = "unavailable"
        else:
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
    """Remove an episode's .mkv and its sidecars, ignoring already-gone files."""
    for p in naming.episode_files(mkv_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


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
                # Idle: reconcile per-user libraries against current
                # subscriptions, backfill podcast audio for channels newly
                # opted in, and prune anything no user retains.
                reconcile_links(db, media_root)
                backfill_audio(db, settings)
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
