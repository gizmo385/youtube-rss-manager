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


def start_scheduler() -> None:
    scheduler.add_job(
        sync_all_accounts,
        "interval",
        hours=6,
        id="sync_all",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Subscription sync scheduler started (every 6h)")


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)
