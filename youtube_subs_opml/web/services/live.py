from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import VideoLiveStatus

logger = logging.getLogger(__name__)

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
_TIMEOUT = 10.0
# How long to trust a transient (upcoming/live) verdict before re-probing.
_TRANSIENT_TTL = timedelta(minutes=15)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STATUS_NONE = "none"
STATUS_UPCOMING = "upcoming"
STATUS_LIVE = "live"
# Statuses we'd hide; also the ones that aren't terminal and need re-checking.
TRANSIENT_STATUSES = frozenset({STATUS_UPCOMING, STATUS_LIVE})


def resolve_include_live(
    sub_pref: bool | None,
    cat_pref: bool | None,
    user_pref: bool,
) -> bool:
    """Cascade: subscription > category > user. NULL means inherit.

    Returns whether premieres/livestreams should be *included* while still
    upcoming or live (mirrors include_shorts).
    """
    if sub_pref is not None:
        return sub_pref
    if cat_pref is not None:
        return cat_pref
    return user_pref


def _is_stale(checked_at: datetime, now: datetime) -> bool:
    if checked_at.tzinfo is None:  # SQLite may return naive datetimes
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return now - checked_at > _TRANSIENT_TTL


def _probe_live_status(video_id: str, client: httpx.Client) -> str | None:
    """Read a video's live/premiere status from its watch page.

    Returns 'upcoming', 'live', or 'none', or None if the probe was
    inconclusive (network/HTTP error) so the caller can fail open.
    """
    try:
        resp = client.get(
            WATCH_URL.format(video_id=video_id),
            follow_redirects=True,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Live probe failed for %s: %s", video_id, exc)
        return None

    text = resp.text
    if '"isUpcoming":true' in text:
        return STATUS_UPCOMING
    if '"isLiveNow":true' in text or '"isLive":true' in text:
        return STATUS_LIVE
    return STATUS_NONE


def classify_live(video_ids: list[str], db: Session) -> dict[str, str]:
    """Return ``{video_id: status}`` for the given ids.

    Caching: a terminal 'none' verdict is kept permanently; 'upcoming'/'live'
    are re-probed once older than the TTL (they transition over time).
    Inconclusive probes are omitted from the result so callers fail open.
    """
    if not video_ids:
        return {}

    rows = (
        db.execute(
            select(VideoLiveStatus).where(VideoLiveStatus.video_id.in_(video_ids))
        )
        .scalars()
        .all()
    )
    cached = {row.video_id: row for row in rows}

    now = datetime.now(timezone.utc)
    result: dict[str, str] = {}
    to_probe: list[str] = []
    for vid in video_ids:
        row = cached.get(vid)
        if row is None:
            to_probe.append(vid)
        elif row.status in TRANSIENT_STATUSES and _is_stale(row.checked_at, now):
            to_probe.append(vid)
        else:
            result[vid] = row.status

    if to_probe:
        with httpx.Client(headers={"User-Agent": _USER_AGENT}) as client:
            for vid in to_probe:
                status = _probe_live_status(vid, client)
                if status is None:
                    continue
                row = cached.get(vid)
                if row is None:
                    db.add(
                        VideoLiveStatus(video_id=vid, status=status, checked_at=now)
                    )
                else:
                    row.status = status
                    row.checked_at = now
                result[vid] = status
        db.commit()

    return result
