from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from ..config import get_settings
from ..db import get_session_factory
from ..models import YoutubeAccount
from .sync import sync_account

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def sync_all_accounts() -> None:
    """Scheduled job: sync every YoutubeAccount."""
    db = get_session_factory()()
    settings = get_settings()
    try:
        accounts = db.execute(select(YoutubeAccount)).scalars().all()
        for account in accounts:
            try:
                count = sync_account(account, db, settings)
                db.commit()
                logger.info(
                    "Synced account %s (%s): %d subs",
                    account.id,
                    account.channel_title,
                    count,
                )
            except Exception:
                logger.exception("Sync failed for account %s", account.id)
                db.rollback()
    finally:
        db.close()


def poll_videos() -> None:
    """Scheduled job: persist new videos from channel RSS and enqueue downloads.

    Cheap enough to live in the web process — it's HTTP fetches and inserts,
    not media. The actual downloading runs in a separate container.
    """
    from .poller import poll_all_channels

    db = get_session_factory()()
    try:
        poll_all_channels(db)
    except Exception:
        logger.exception("Video poll failed")
        db.rollback()
    finally:
        db.close()


def sync_jellyfin() -> None:
    """Scheduled job: resolve item ids and reconcile Jellyfin playlists.

    HTTP + DB only (no media I/O), so it lives in the web process. A no-op for
    users without a Jellyfin account configured.
    """
    from .jellyfin_sync import sync_all

    db = get_session_factory()()
    try:
        sync_all(db)
    except Exception:
        logger.exception("Jellyfin sync failed")
        db.rollback()
    finally:
        db.close()


def start_scheduler() -> None:
    scheduler.add_job(
        sync_all_accounts,
        "interval",
        hours=6,
        id="sync_all",
        replace_existing=True,
    )
    settings = get_settings()
    interval = settings.poll_interval_minutes
    scheduler.add_job(
        poll_videos,
        "interval",
        minutes=interval,
        id="poll_videos",
        replace_existing=True,
    )
    jellyfin_interval = settings.jellyfin_sync_interval_minutes
    scheduler.add_job(
        sync_jellyfin,
        "interval",
        minutes=jellyfin_interval,
        id="sync_jellyfin",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Schedulers started (subscription sync every 6h, video poll every %dm, "
        "Jellyfin sync every %dm)",
        interval,
        jellyfin_interval,
    )


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
