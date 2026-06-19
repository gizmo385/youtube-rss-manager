from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import VideoShort

logger = logging.getLogger(__name__)

SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"
_PROBE_TIMEOUT = 10.0
# A browser-ish UA: YouTube serves bots inconsistently for the /shorts/ path.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def resolve_include_shorts(
    sub_pref: bool | None,
    cat_pref: bool | None,
    user_pref: bool,
) -> bool:
    """Cascade: subscription > category > user. NULL means inherit."""
    if sub_pref is not None:
        return sub_pref
    if cat_pref is not None:
        return cat_pref
    return user_pref


def _probe_is_short(video_id: str, client: httpx.Client) -> bool | None:
    """Probe whether a video id is a Short.

    Requesting ``youtube.com/shorts/{id}`` with redirects disabled: a real
    Short responds 200, a regular video 30x-redirects to ``/watch``. Returns
    True/False, or None if the probe was inconclusive (network/HTTP error or
    an unexpected status), in which case the caller should fail open.

    Uses a streaming GET that never reads the body, so we pay only for headers
    (HEAD is unreliable here — YouTube does not always redirect for it).
    """
    url = SHORTS_URL.format(video_id=video_id)
    try:
        with client.stream(
            "GET", url, follow_redirects=False, timeout=_PROBE_TIMEOUT
        ) as resp:
            if resp.status_code == 200:
                return True
            if resp.is_redirect:
                return False
            logger.warning(
                "Unexpected shorts-probe status %s for %s", resp.status_code, video_id
            )
            return None
    except httpx.HTTPError as exc:
        logger.warning("Shorts probe failed for %s: %s", video_id, exc)
        return None


def classify_videos(video_ids: list[str], db: Session) -> dict[str, bool]:
    """Return ``{video_id: is_short}``, probing and caching any cache misses.

    Inconclusive probes are left out of the result (and not cached) so callers
    fail open — an undetermined video is kept in the feed rather than dropped.
    """
    if not video_ids:
        return {}

    rows = (
        db.execute(select(VideoShort).where(VideoShort.video_id.in_(video_ids)))
        .scalars()
        .all()
    )
    result: dict[str, bool] = {row.video_id: row.is_short for row in rows}

    missing = [vid for vid in video_ids if vid not in result]
    if missing:
        with httpx.Client(headers={"User-Agent": _USER_AGENT}) as client:
            for vid in missing:
                verdict = _probe_is_short(vid, client)
                if verdict is None:
                    continue
                db.add(VideoShort(video_id=vid, is_short=verdict))
                result[vid] = verdict
        db.commit()

    return result
