from __future__ import annotations

import logging
from datetime import datetime

import httpx
from sqlalchemy import select, union
from sqlalchemy.orm import Session

from ..models import Category, ChannelCategory, PlaylistVideo, Subscription, User, Video, VideoShort

logger = logging.getLogger(__name__)

SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"
_PROBE_TIMEOUT = 10.0
# A browser-ish UA: YouTube serves bots inconsistently for the /shorts/ path.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
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
        with client.stream("GET", url, follow_redirects=False, timeout=_PROBE_TIMEOUT) as resp:
            if resp.status_code == 200:
                return True
            if resp.is_redirect:
                return False
            logger.warning("Unexpected shorts-probe status %s for %s", resp.status_code, video_id)
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

    rows = db.execute(select(VideoShort).where(VideoShort.video_id.in_(video_ids))).scalars().all()
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


def channels_hiding_shorts(db: Session) -> set[str]:
    """Ids of the channels and playlists some follower hides Shorts from.

    Placement by placement, as the Watch tab applies the preferences: a
    channel in two categories hides its Shorts for a follower if either
    category does.
    """
    users = {u.id: u.include_shorts for u in db.execute(select(User)).scalars()}
    cat_prefs: dict[tuple[int, str], list[bool | None]] = {}
    for user_id, channel_id, pref in db.execute(
        select(ChannelCategory.user_id, ChannelCategory.channel_id, Category.include_shorts).join(
            Category, Category.id == ChannelCategory.category_id
        )
    ).tuples():
        cat_prefs.setdefault((user_id, channel_id), []).append(pref)

    hiding: set[str] = set()
    for sub in db.execute(select(Subscription).where(Subscription.ignored.is_(False))).scalars():
        if sub.channel_id in hiding or sub.user_id not in users:
            continue
        cats = cat_prefs.get((sub.user_id, sub.channel_id)) or [None]
        if any(not resolve_include_shorts(sub.include_shorts, cat, users[sub.user_id]) for cat in cats):
            hiding.add(sub.channel_id)
    return hiding


def classify_recent(db: Session, since: datetime, limit: int = 200) -> int:
    """Probe the unclassified videos published since ``since`` on channels and
    playlists that some follower hides Shorts from, newest first, at most
    ``limit`` of them. Returns how many turned out to be Shorts.

    The Watch tab, channel pages and search only consult ``video_shorts`` and
    show what isn't in it, so this is what keeps Shorts the feed didn't link as
    such off them. Runs after each poll; whatever is over ``limit`` waits for
    the next one, and an inconclusive probe is retried then too.
    """
    sources = channels_hiding_shorts(db)
    if not sources:
        return 0
    unclassified = Video.video_id.not_in(select(VideoShort.video_id))
    recent = Video.published_at >= since
    candidates = union(
        select(Video.video_id, Video.published_at).where(Video.channel_id.in_(sources), recent, unclassified),
        select(Video.video_id, Video.published_at)
        .join(PlaylistVideo, PlaylistVideo.video_id == Video.video_id)
        .where(PlaylistVideo.playlist_id.in_(sources), recent, unclassified),
    ).subquery()
    ids = list(
        db.execute(select(candidates.c.video_id).order_by(candidates.c.published_at.desc()).limit(limit)).scalars()
    )
    if not ids:
        return 0
    verdicts = classify_videos(ids, db)
    found = sum(1 for v in ids if verdicts.get(v))
    logger.info("Classified %d recent videos, %d of them Shorts", len(verdicts), found)
    return found
