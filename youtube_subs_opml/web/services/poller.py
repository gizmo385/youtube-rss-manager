"""Scheduled poller: persist videos from channel RSS.

Deliberately separate from ``routes/feed.py``. The feed proxy is pull-driven —
it only runs when an RSS reader requests a channel — which would tie the
archive to the reader's schedule and to which OPML feeds happen to be
subscribed. Unsubscribing a category in FreshRSS would silently stop downloads.

This runs on its own interval and upserts into ``videos`` regardless.

Known bound: YouTube's channel feed returns only the ~15 most recent entries.
A channel publishing more than that between polls loses the overflow
permanently, which is why ``poll_interval_minutes`` defaults to 20 rather than
matching the 6-hour subscription sync.
"""

from __future__ import annotations

import logging
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from youtube_subs_opml.opml import FEED_URL

from ..models import Subscription, Video
from .archive import enqueue_pending

logger = logging.getLogger(__name__)

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_TIMEOUT = 15.0


def _parse_entries(xml_bytes: bytes) -> list[tuple[str, str, datetime | None]]:
    """Extract (video_id, title, published) from a channel feed."""
    root = ET.fromstring(xml_bytes)
    out: list[tuple[str, str, datetime | None]] = []
    for entry in root.findall(f"{{{_ATOM}}}entry"):
        vid_el = entry.find(f"{{{_YT}}}videoId")
        if vid_el is None or not vid_el.text:
            continue
        title_el = entry.find(f"{{{_ATOM}}}title")
        pub_el = entry.find(f"{{{_ATOM}}}published")
        published: datetime | None = None
        if pub_el is not None and pub_el.text:
            try:
                published = datetime.fromisoformat(pub_el.text)
            except ValueError:
                logger.warning("Unparseable published date: %s", pub_el.text)
        out.append((vid_el.text, (title_el.text or "") if title_el is not None else "", published))
    return out


def poll_channel(channel_id: str, db: Session) -> int:
    """Fetch one channel's feed and upsert its videos. Returns new video count."""
    try:
        resp = httpx.get(
            FEED_URL.format(channel_id=channel_id),
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Poll failed for %s: %s", channel_id, exc)
        return 0

    entries = _parse_entries(resp.content)
    if not entries:
        return 0

    ids = [e[0] for e in entries]
    known = set(
        db.execute(select(Video.video_id).where(Video.video_id.in_(ids)))
        .scalars()
        .all()
    )

    new = 0
    for video_id, title, published in entries:
        if video_id in known:
            continue
        db.add(
            Video(
                video_id=video_id,
                channel_id=channel_id,
                title=title,
                published_at=published,
            )
        )
        new += 1
    return new


def poll_all_channels(db: Session) -> int:
    """Poll every non-ignored subscribed channel, then enqueue downloads.

    Shorts and live filtering are intentionally *not* applied here — videos are
    recorded unconditionally so the feed proxy keeps full control of what gets
    surfaced. Whether a video is downloaded is a separate decision made in
    ``services.archive``.
    """
    channel_ids = set(
        db.execute(
            select(Subscription.channel_id).where(
                Subscription.ignored == False  # noqa: E712
            )
        )
        .scalars()
        .all()
    )

    total = 0
    for channel_id in sorted(channel_ids):
        try:
            total += poll_channel(channel_id, db)
            db.commit()
        except Exception:
            logger.exception("Poll failed for channel %s", channel_id)
            db.rollback()

    try:
        enqueue_pending(db)
        db.commit()
    except Exception:
        logger.exception("Enqueue failed")
        db.rollback()

    logger.info("Polled %d channels, %d new videos", len(channel_ids), total)
    return total
