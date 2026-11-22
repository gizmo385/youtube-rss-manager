"""Video lengths from the YouTube Data API, for videos the downloader hasn't
probed.

The RSS feed doesn't carry a video's length, so until now only archived videos
(probed by yt-dlp before downloading) had one, and the Watch tab showed no
length on anything else. ``videos.list`` with ``part=contentDetails`` takes up
to 50 ids a call at 1 quota unit, so a day of new uploads costs a unit or two,
and the backlog (including the history backfill's older videos) drains within
a daily budget of its own.

Runs after each poll, so new uploads get a length within the same sweep.
Videos the poller found on a feed come first, newest first, then backfilled
ones. The downloader's probe still overwrites whatever is stored here when it
archives a video, so what it checks duration limits against is its own.

Ids the API doesn't return (private, deleted, or region-blocked everywhere)
are remembered in memory and not asked about again until a restart. Upcoming
premieres and live streams report no length yet; they're left for a later run.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Video
from .history import _quota_day
from .uploads_api import error_reason

logger = logging.getLogger(__name__)

_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
BATCH = 50
# Calls (quota units) per run: a run stays short, and the backlog is spread
# over the day's polls rather than spent in one burst.
CALLS_PER_RUN = 20

_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")

# Units spent per quota day, days YouTube said the quota had run out, and ids
# the API didn't return. In memory, like the history backfill's.
_spent: dict[date, int] = {}
_exhausted: set[date] = set()
_missing: set[str] = set()


def parse_duration(value: str) -> int | None:
    """Seconds in an ISO 8601 duration like ``PT1H2M3S``; None for anything
    else, and for ``P0D``, which is what a not-yet-started stream reports."""
    match = _ISO_DURATION.match(value or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    return total or None


def fetch_durations(video_ids: list[str], client: httpx.Client, api_key: str) -> dict[str, int | None]:
    """Each returned id's length in seconds (None while it has none yet). Ids
    the API leaves out are absent. Raises ``httpx.HTTPError`` on failure."""
    resp = client.get(
        _VIDEOS_URL,
        params={"part": "contentDetails", "id": ",".join(video_ids), "maxResults": BATCH},
        headers={"X-Goog-Api-Key": api_key},
    )
    resp.raise_for_status()
    return {
        item["id"]: parse_duration(item.get("contentDetails", {}).get("duration", ""))
        for item in resp.json().get("items", [])
        if item.get("id")
    }


def fill_durations(db: Session, settings: Settings, client: httpx.Client) -> int:
    """One run. Returns the number of videos given a length."""
    if not settings.youtube_api_key or settings.youtube_duration_daily_units <= 0:
        return 0
    day = _quota_day()
    if day in _exhausted:
        return 0
    budget = min(CALLS_PER_RUN, settings.youtube_duration_daily_units - _spent.get(day, 0))
    if budget <= 0:
        return 0

    # Fetched in one go and filtered here rather than in SQL, so the ids the
    # API never returns don't make the query grow.
    ids = (
        db.execute(
            select(Video.video_id)
            .where(Video.duration_seconds.is_(None))
            .order_by(Video.backfilled.asc(), Video.published_at.desc().nulls_last())
            .limit(budget * BATCH + len(_missing))
        )
        .scalars()
        .all()
    )
    ids = [i for i in ids if i not in _missing][: budget * BATCH]

    filled = 0
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        _spent[day] = _spent.get(day, 0) + 1  # failed calls cost quota too
        try:
            found = fetch_durations(batch, client, settings.youtube_api_key)
        except httpx.HTTPStatusError as exc:
            reason = error_reason(exc.response)
            if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                logger.warning("Video lengths paused: YouTube API quota exhausted (%s)", reason)
                _exhausted.add(day)
            else:
                logger.warning("Video length lookup failed: %s %s", exc, reason)
            break
        except httpx.HTTPError as exc:
            logger.warning("Video length lookup failed: %s", exc)
            break
        _missing.update(i for i in batch if i not in found)
        lengths = {i: s for i, s in found.items() if s is not None}
        if lengths:
            # Only where still unset: the downloader may have probed it meanwhile.
            for video in db.execute(
                select(Video).where(Video.video_id.in_(lengths), Video.duration_seconds.is_(None))
            ).scalars():
                video.duration_seconds = lengths[video.video_id]
                filled += 1
        db.commit()

    if filled:
        logger.info("Filled in the length of %d videos from the Data API", filled)
    return filled
