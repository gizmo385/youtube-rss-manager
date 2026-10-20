"""Channel history backfill: each channel's older uploads, beyond the ~15 its
RSS feed carries, so search covers whole channels.

Pages back through a channel's uploads playlist with the Data API — 1 quota
unit per 50 videos, with exact upload dates and full descriptions, which
yt-dlp's quota-free channel listing lacks. A scheduled job works through the
subscribed YouTube channels a few pages at a time, resuming where it left off
(``Channel.backfill_page_token``) and marking each one done when it reaches
the end (``Channel.backfilled_at``), within a daily quota budget. New uploads
after that come from the poller as usual.

What it adds is flagged ``Video.backfilled``: searchable and shown on channel
pages, but never archived — otherwise a channel set to keep everything would
queue its whole back catalogue for download. The poller clears the flag on
anything that turns up on the feed, since the backfill can reach a brand-new
upload before the poller does.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Channel, Subscription, Video
from .uploads_api import error_reason, fetch_uploads_page

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
# Pages (quota units) per run: a run stays short, and the daily budget is
# spread over the day rather than spent in one burst.
PAGES_PER_RUN = 50

# The Data API's quota day ends at midnight Pacific.
_QUOTA_TZ = ZoneInfo("America/Los_Angeles")

# Units spent per quota day, and days YouTube said the quota had run out. In
# memory: a restart forgets them, which at worst spends one extra run's worth.
_spent: dict[date, int] = {}
_exhausted: set[date] = set()


def _quota_day() -> date:
    return datetime.now(_QUOTA_TZ).date()


def backfill_history(db: Session, settings: Settings, client: httpx.Client) -> int:
    """One run of the backfill. Returns the number of videos added."""
    if not settings.youtube_api_key or settings.youtube_backfill_daily_units <= 0:
        return 0
    day = _quota_day()
    if day in _exhausted:
        return 0
    budget = min(PAGES_PER_RUN, settings.youtube_backfill_daily_units - _spent.get(day, 0))
    if budget <= 0:
        return 0

    followed = select(Subscription.channel_id).where(Subscription.ignored.is_(False))
    channels = (
        db.execute(
            select(Channel)
            .where(
                Channel.platform == "youtube",
                Channel.backfilled_at.is_(None),
                Channel.channel_id.in_(followed),
            )
            .order_by(Channel.channel_id)
        )
        .scalars()
        .all()
    )

    added = 0
    for channel in channels:
        while budget > 0 and channel.backfilled_at is None:
            budget -= 1
            _spent[day] = _spent.get(day, 0) + 1  # failed calls cost quota too
            try:
                items, next_token = fetch_uploads_page(
                    channel.channel_id,
                    client,
                    settings.youtube_api_key,
                    channel.backfill_page_token,
                    page_size=PAGE_SIZE,
                )
            except httpx.HTTPStatusError as exc:
                reason = error_reason(exc.response)
                if exc.response.status_code == 404:
                    # No uploads playlist: a channel that's never uploaded, or gone.
                    channel.backfilled_at = datetime.now(UTC)
                    db.commit()
                    break
                if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    logger.warning("History backfill paused: YouTube API quota exhausted (%s)", reason)
                    _exhausted.add(day)
                else:
                    logger.warning("History backfill failed for %s: %s %s", channel.channel_id, exc, reason)
                return _done(added)
            except httpx.HTTPError as exc:
                logger.warning("History backfill failed for %s: %s", channel.channel_id, exc)
                return _done(added)

            added += _store(db, channel.channel_id, items)
            channel.backfill_page_token = next_token
            if not next_token:
                channel.backfilled_at = datetime.now(UTC)
            db.commit()
        if budget <= 0:
            break
    return _done(added)


def _done(added: int) -> int:
    if added:
        logger.info("History backfill added %d older videos", added)
    return added


def _store(db: Session, channel_id: str, items: list[dict]) -> int:
    """Add a page's unknown videos, flagged ``backfilled``, and fill in the
    description of known ones that lack it. Returns the number added."""
    entries: dict[str, tuple[str, datetime, str]] = {}
    for item in items:
        snippet = item.get("snippet", {})
        video_id = snippet.get("resourceId", {}).get("videoId")
        # Private and deleted videos linger in the playlist without an owner.
        if not video_id or "videoOwnerChannelId" not in snippet:
            continue
        published = item.get("contentDetails", {}).get("videoPublishedAt") or snippet.get("publishedAt")
        if not published:
            continue
        title, description = snippet.get("title", ""), snippet.get("description", "")
        entries[video_id] = (title, datetime.fromisoformat(published), description)
    if not entries:
        return 0

    known = {v.video_id: v for v in db.execute(select(Video).where(Video.video_id.in_(list(entries)))).scalars()}
    added = 0
    for video_id, (title, published, description) in entries.items():
        video = known.get(video_id)
        if video is None:
            db.add(
                Video(
                    video_id=video_id,
                    channel_id=channel_id,
                    title=title[:512],
                    published_at=published,
                    description=description,
                    backfilled=True,
                )
            )
            added += 1
        elif video.description is None:
            video.description = description
    return added
