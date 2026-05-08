from __future__ import annotations

import logging

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from youtube_subs_opml.youtube import fetch_channel_topics, fetch_subscriptions

from ..config import Settings
from ..models import Channel, Subscription, YoutubeAccount
from .crypto import decrypt_token

logger = logging.getLogger(__name__)


def build_google_credentials(
    refresh_token: str, settings: Settings
) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
    )


def sync_account(account: YoutubeAccount, db: Session, settings: Settings) -> int:
    """Sync subscriptions for one YoutubeAccount. Returns count of subs found."""
    refresh_token = decrypt_token(account.refresh_token_encrypted)
    creds = build_google_credentials(refresh_token, settings)

    try:
        fetched = fetch_subscriptions(creds)
    except RefreshError:
        logger.error(
            "Refresh token revoked for account %s (channel %s). "
            "User needs to re-connect.",
            account.id,
            account.channel_id,
        )
        raise

    # Fetch topic categories for all subscribed channels
    channel_ids = [s.channel_id for s in fetched]
    try:
        topics_map = fetch_channel_topics(creds, channel_ids)
    except Exception:
        logger.warning("Failed to fetch channel topics, continuing without them")
        topics_map = {}

    # Upsert channels
    for sub in fetched:
        channel = db.get(Channel, sub.channel_id)
        topics = topics_map.get(sub.channel_id)
        if channel is None:
            db.add(
                Channel(
                    channel_id=sub.channel_id,
                    title=sub.title,
                    description=sub.description,
                    youtube_topics=topics,
                )
            )
        else:
            channel.title = sub.title
            channel.description = sub.description
            channel.youtube_topics = topics
            channel.last_seen_at = func.now()

    # Reconcile subscriptions
    fetched_ids = {s.channel_id for s in fetched}

    existing = db.execute(
        select(Subscription).where(
            Subscription.user_id == account.user_id,
            Subscription.account_id == account.id,
        )
    ).scalars().all()
    existing_ids = {s.channel_id for s in existing}

    # Add new subscriptions
    for sub in fetched:
        if sub.channel_id not in existing_ids:
            db.add(
                Subscription(
                    user_id=account.user_id,
                    channel_id=sub.channel_id,
                    account_id=account.id,
                )
            )

    # Remove stale subscriptions (unsubscribed on YouTube)
    for s in existing:
        if s.channel_id not in fetched_ids:
            db.delete(s)

    account.last_synced_at = func.now()
    db.flush()

    return len(fetched)
