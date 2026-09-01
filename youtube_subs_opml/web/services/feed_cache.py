"""Read/write the per-channel raw feed cache.

The poller warms it on its spread-out sweep; the feed proxy serves from it so a
reader polling every URL at once never fans out to YouTube. Neither helper
commits — the caller owns the transaction (the poller commits per channel, the
proxy commits its one-off seed).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import ChannelFeedCache


def store_feed(db: Session, channel_id: str, xml: bytes) -> None:
    """Upsert the cached XML for a channel. A no-op when unchanged, so a channel
    with no new uploads doesn't churn a write on every poll."""
    row = db.get(ChannelFeedCache, channel_id)
    if row is None:
        db.add(
            ChannelFeedCache(
                channel_id=channel_id, xml=xml, fetched_at=datetime.now(timezone.utc)
            )
        )
    elif row.xml != xml:
        row.xml = xml
        row.fetched_at = datetime.now(timezone.utc)


def load_feed(db: Session, channel_id: str) -> bytes | None:
    """The last cached XML for a channel, or None if it's never been polled."""
    row = db.get(ChannelFeedCache, channel_id)
    return row.xml if row else None
