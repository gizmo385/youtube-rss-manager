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
import random
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from youtube_subs_opml.opml import FEED_URL

from ..config import get_settings
from ..models import Subscription, Video
from .archive import enqueue_pending
from .feed_cache import store_feed

logger = logging.getLogger(__name__)

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_TIMEOUT = 15.0

# YouTube is noticeably friendlier to a browser-like UA than to httpx's default.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# Statuses YouTube returns when soft-throttling a busy IP. 404 is included
# because a throttled feed 404s intermittently (the same channel returns 200
# moments later), so a retry usually clears it; a genuinely dead channel just
# costs a couple of extra requests per sweep.
_RETRY_STATUSES = frozenset({404, 429, 500, 502, 503})


def _new_client() -> httpx.Client:
    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )


def _sleep_backoff(base_delay: float, attempt: int) -> None:
    """Exponential backoff with jitter. A no-op when base_delay is 0."""
    if base_delay <= 0:
        return
    time.sleep(base_delay * (2 ** attempt) + random.uniform(0, base_delay))


def _fetch_feed(
    channel_id: str, client: httpx.Client, *, max_retries: int, base_delay: float
) -> httpx.Response:
    """Fetch one channel feed, retrying transient throttling responses.

    Raises ``httpx.HTTPError`` once retries are exhausted, for the caller to log.
    """
    url = FEED_URL.format(channel_id=channel_id)
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url)
        except httpx.RequestError:
            if attempt >= max_retries:
                raise
            _sleep_backoff(base_delay, attempt)
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
            _sleep_backoff(base_delay, attempt)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")  # pragma: no cover


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


def poll_channel(
    channel_id: str, db: Session, client: httpx.Client | None = None
) -> int:
    """Fetch one channel's feed and upsert its videos. Returns new video count.

    Pass a shared ``client`` when polling many channels; otherwise a throwaway
    one is created so callers (e.g. a manual single-channel poll) stay simple.
    """
    settings = get_settings()
    owns_client = client is None
    client = client or _new_client()
    try:
        resp = _fetch_feed(
            channel_id,
            client,
            max_retries=settings.poll_max_retries,
            base_delay=settings.poll_channel_delay_seconds,
        )
    except httpx.HTTPError as exc:
        logger.warning("Poll failed for %s: %s", channel_id, exc)
        return 0
    finally:
        if owns_client:
            client.close()

    # Warm the feed cache so the proxy can serve readers without hitting YouTube.
    # Cached regardless of whether there are new videos — an unchanged feed is
    # still what a reader should get. store_feed skips the write when unchanged.
    store_feed(db, channel_id, resp.content)

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
    settings = get_settings()
    delay = settings.poll_channel_delay_seconds

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
    client = _new_client()
    try:
        for i, channel_id in enumerate(sorted(channel_ids)):
            # Space out the sweep so ~50 channels don't look like a burst.
            if i and delay > 0:
                time.sleep(delay + random.uniform(0, delay))
            try:
                total += poll_channel(channel_id, db, client)
                db.commit()
            except Exception:
                logger.exception("Poll failed for channel %s", channel_id)
                db.rollback()
    finally:
        client.close()

    try:
        enqueue_pending(db)
        db.commit()
    except Exception:
        logger.exception("Enqueue failed")
        db.rollback()

    logger.info("Polled %d channels, %d new videos", len(channel_ids), total)
    return total
