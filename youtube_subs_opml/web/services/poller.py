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

When a YouTube feed fails even after retries, and ``YOUTUBE_API_KEY`` is set, the
channel's uploads are read from the Data API instead (see ``uploads_api``), at
most once per channel per ``youtube_api_fallback_interval_minutes``.

Nebula channels are polled on the same sweep, but only to warm the feed cache:
nothing on Nebula is downloadable, so their entries aren't recorded as videos.
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

from youtube_subs_opml import metrics, nebula
from youtube_subs_opml.opml import FEED_URL

from ..config import get_settings
from ..models import Channel, ChannelFeedCache, Subscription, Video
from . import uploads_api
from .archive import enqueue_pending
from .feed_cache import store_feed

logger = logging.getLogger(__name__)

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_TIMEOUT = 15.0

# YouTube is noticeably friendlier to a browser-like UA than to httpx's default.
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Statuses YouTube returns when soft-throttling a busy IP. 404 is included
# because a throttled feed 404s intermittently (the same channel returns 200
# moments later), so a retry usually clears it; a genuinely dead channel just
# costs a couple of extra requests per sweep.
_RETRY_STATUSES = frozenset({404, 429, 500, 502, 503})

# channel_id -> time.monotonic() of its last Data API fallback. In memory: the
# web app is a single process, and a restart costs at most one extra quota unit
# per channel.
_api_fallback_at: dict[str, float] = {}


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
    time.sleep(base_delay * (2**attempt) + random.uniform(0, base_delay))


def _fetch_feed(
    url: str,
    client: httpx.Client,
    *,
    max_retries: int,
    base_delay: float,
    platform: str = "youtube",
) -> httpx.Response:
    """Fetch one channel feed, retrying transient throttling responses.

    Every attempt is counted in ``yt_rss_feed_fetch_attempts``, so throttling
    that a retry papers over still shows up.

    Raises ``httpx.HTTPError`` once retries are exhausted, for the caller to log.
    """
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url)
        except httpx.RequestError:
            metrics.feed_fetch_attempts.add(
                1, {"platform": platform, "status": "network_error", "attempt": attempt + 1}
            )
            if attempt >= max_retries:
                raise
            _sleep_backoff(base_delay, attempt)
            continue
        metrics.feed_fetch_attempts.add(
            1, {"platform": platform, "status": str(resp.status_code), "attempt": attempt + 1}
        )
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


def poll_channel(channel_id: str, db: Session, client: httpx.Client | None = None) -> int:
    """Fetch one channel's feed and upsert its videos. Returns new video count.

    Pass a shared ``client`` when polling many channels; otherwise a throwaway
    one is created so callers (e.g. a manual single-channel poll) stay simple.
    """
    settings = get_settings()
    channel = db.get(Channel, channel_id)
    is_nebula = channel is not None and channel.platform == nebula.PLATFORM
    url = nebula.feed_url(channel_id) if is_nebula else FEED_URL.format(channel_id=channel_id)
    platform = nebula.PLATFORM if is_nebula else "youtube"
    channel_attrs = {
        "platform": platform,
        "channel_id": channel_id,
        "channel": (channel.title if channel is not None else "") or channel_id,
    }
    owns_client = client is None
    client = client or _new_client()
    started = time.monotonic()
    try:
        resp = _fetch_feed(
            url,
            client,
            max_retries=settings.poll_max_retries,
            base_delay=settings.poll_channel_delay_seconds,
            platform=platform,
        )
    except httpx.HTTPError as exc:
        logger.warning("Poll failed for %s: %s", channel_id, exc)
        if isinstance(exc, httpx.HTTPStatusError):
            outcome, status = "http_error", str(exc.response.status_code)
        else:
            outcome, status = "network_error", "network_error"
        _record_poll(channel_attrs, outcome, status, started)
        content = None if is_nebula else _api_fallback(channel_id, channel_attrs, client)
        if content is None:
            return 0
    else:
        _record_poll(channel_attrs, "ok", str(resp.status_code), started)
        content = resp.content
    finally:
        if owns_client:
            client.close()

    # Warm the feed cache so the proxy can serve readers without hitting YouTube.
    # Cached regardless of whether there are new videos — an unchanged feed is
    # still what a reader should get. store_feed skips the write when unchanged.
    store_feed(db, channel_id, content)
    if is_nebula:
        return 0

    entries = _parse_entries(content)
    if not entries:
        return 0

    ids = [e[0] for e in entries]
    known = set(db.execute(select(Video.video_id).where(Video.video_id.in_(ids))).scalars().all())

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
    if new:
        metrics.feed_new_videos.add(new, channel_attrs)
    return new


def _record_poll(channel_attrs: dict[str, str], outcome: str, status: str, started: float) -> None:
    metrics.feed_polls.add(1, {**channel_attrs, "outcome": outcome, "status": status})
    metrics.feed_poll_duration.record(
        time.monotonic() - started,
        {"platform": channel_attrs["platform"], "outcome": outcome},
    )


def _api_fallback(channel_id: str, channel_attrs: dict[str, str], client: httpx.Client) -> bytes | None:
    """The channel's uploads from the Data API, after its RSS feed failed.

    None when no API key is configured, when this channel already fell back
    within ``youtube_api_fallback_interval_minutes``, or when the API fails too.
    A failed call still counts toward the interval, so a bad key or an exhausted
    quota isn't retried on every sweep.
    """
    settings = get_settings()
    if not settings.youtube_api_key:
        return None
    now = time.monotonic()
    last = _api_fallback_at.get(channel_id)
    if last is not None and now - last < settings.youtube_api_fallback_interval_minutes * 60:
        metrics.feed_api_fallbacks.add(1, {**channel_attrs, "outcome": "skipped"})
        return None
    _api_fallback_at[channel_id] = now
    try:
        content = uploads_api.fetch_uploads_feed(
            channel_id, client, settings.youtube_api_key, title=channel_attrs["channel"]
        )
    except httpx.HTTPError as exc:
        reason = uploads_api.error_reason(exc.response) if isinstance(exc, httpx.HTTPStatusError) else ""
        logger.warning(
            "Data API fallback failed for %s: %s%s",
            channel_id,
            exc,
            f" ({reason})" if reason else "",
        )
        metrics.feed_api_fallbacks.add(1, {**channel_attrs, "outcome": "error"})
        return None
    logger.info("Polled %s via the Data API after its RSS feed failed", channel_id)
    metrics.feed_api_fallbacks.add(1, {**channel_attrs, "outcome": "ok"})
    return content


def _poll_one(channel_id: str, db: Session, client: httpx.Client) -> int:
    """Poll one channel in its own transaction, so a failure can't sink the sweep."""
    try:
        new = poll_channel(channel_id, db, client)
        db.commit()
        return new
    except Exception:
        logger.exception("Poll failed for channel %s", channel_id)
        db.rollback()
        return 0


def _uncached_channel_ids(db: Session) -> set[str]:
    """Subscribed, non-ignored channels with no cached feed yet."""
    return set(
        db.execute(
            select(Subscription.channel_id)
            .outerjoin(
                ChannelFeedCache,
                ChannelFeedCache.channel_id == Subscription.channel_id,
            )
            .where(
                Subscription.ignored == False,  # noqa: E712
                ChannelFeedCache.channel_id.is_(None),
            )
        )
        .scalars()
        .all()
    )


def warm_uncached_channels(db: Session) -> int:
    """Poll only the channels whose feed has never been cached.

    Runs as a one-off job after a channel is added or an account synced, so a
    new feed URL stops 503ing in seconds. The alternative, nudging the full
    sweep, can't help while a sweep is already running: that sweep took its
    channel list when it started, and a throttled sweep can run for half an hour.

    Re-checks for uncached channels until none are left, so a channel added
    while this job is running is picked up by it too. Each channel is tried at
    most once per run; a failure is left to the regular sweep. Spaced like the
    sweep, since it can run at the same time and YouTube sees both.
    """
    delay = get_settings().poll_channel_delay_seconds
    attempted: set[str] = set()
    total = 0
    client = _new_client()
    try:
        while pending := sorted(_uncached_channel_ids(db) - attempted):
            for channel_id in pending:
                if attempted and delay > 0:
                    time.sleep(delay + random.uniform(0, delay))
                attempted.add(channel_id)
                # The running sweep may have reached it since the query above.
                if db.get(ChannelFeedCache, channel_id) is not None:
                    continue
                total += _poll_one(channel_id, db, client)
    finally:
        client.close()

    if total:
        try:
            enqueue_pending(db)
            db.commit()
        except Exception:
            logger.exception("Enqueue failed")
            db.rollback()

    logger.info("Warmed %d new channels, %d new videos", len(attempted), total)
    return total


def poll_all_channels(db: Session) -> int:
    """Poll every non-ignored subscribed channel, then enqueue downloads.

    Shorts and live filtering are intentionally *not* applied here — videos are
    recorded unconditionally so the feed proxy keeps full control of what gets
    surfaced. Whether a video is downloaded is a separate decision made in
    ``services.archive``.
    """
    settings = get_settings()
    delay = settings.poll_channel_delay_seconds

    rows = db.execute(
        select(Subscription.channel_id, Channel.platform)
        .outerjoin(Channel, Channel.channel_id == Subscription.channel_id)
        .where(Subscription.ignored == False)  # noqa: E712
        .distinct()
    ).all()
    # Nebula first: its feeds are cheap and never throttled, so they shouldn't
    # queue behind a YouTube sweep that can take half an hour when YouTube is
    # 404ing every request.
    nebula_ids = sorted(cid for cid, platform in rows if platform == nebula.PLATFORM)
    youtube_ids = sorted(cid for cid, platform in rows if platform != nebula.PLATFORM)

    total = 0
    client = _new_client()
    try:
        for channel_id in nebula_ids:
            total += _poll_one(channel_id, db, client)
        for i, channel_id in enumerate(youtube_ids):
            # Space out the YouTube sweep so ~50 channels don't look like a
            # burst. Nebula needs none of this, so it isn't spaced.
            if i and delay > 0:
                time.sleep(delay + random.uniform(0, delay))
            total += _poll_one(channel_id, db, client)
    finally:
        client.close()

    try:
        enqueue_pending(db)
        db.commit()
    except Exception:
        logger.exception("Enqueue failed")
        db.rollback()

    logger.info("Polled %d channels, %d new videos", len(rows), total)
    return total
