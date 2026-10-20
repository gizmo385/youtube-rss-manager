"""SponsorBlock: crowd-sourced segments of YouTube videos — sponsor reads,
self-promotion, "like and subscribe" reminders and so on.

In the in-page player it's a preference that cascades like the others
(subscription > category > user): ``off``; ``show``, which draws the segments
and offers a button to skip each; or ``skip``, which jumps past them. Which
kinds of segment count is a user-level choice.

Archived files are shared between users, so segments are never cut out of
them. The downloader only *marks* them, as chapters Jellyfin shows, when any
subscriber who archives the channel has SponsorBlock on — that adds chapters
and leaves the video alone, so it takes nothing away from anyone else.

The player's segments are fetched when a video is played rather than stored
at download time: they're crowd-sourced, and keep being added and voted on
long after upload. Lookups go by a prefix of the video id's hash, as
SponsorBlock recommends, so the id itself isn't sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Literal, overload

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Category, ChannelCategory, Subscription, User
from .prefs import resolve

logger = logging.getLogger(__name__)

API_URL = "https://sponsor.ajay.app"

MODES: tuple[str, ...] = ("off", "show", "skip")

#: The skippable segment kinds, in the order Settings lists them.
CATEGORIES: dict[str, str] = {
    "sponsor": "Sponsor",
    "selfpromo": "Unpaid / self promotion",
    "interaction": "Interaction reminder (like, subscribe…)",
    "intro": "Intermission / intro animation",
    "outro": "Endcards / credits",
    "preview": "Preview / recap",
    "hook": "Hook / greetings",
    "filler": "Filler tangent",
    "music_offtopic": "Non-music section of a music video",
}

_TIMEOUT = 5.0
# Segments change slowly next to how often a video is reopened. A failed
# lookup is cached briefly too, so an outage isn't retried on every play.
_TTL = 15 * 60
_ERROR_TTL = 60
_CACHE_MAX = 500


@overload
def parse_mode(value: str | None, *, allow_inherit: Literal[False]) -> str: ...


@overload
def parse_mode(value: str | None, *, allow_inherit: bool) -> str | None: ...


def parse_mode(value: str | None, *, allow_inherit: bool) -> str | None:
    """A known mode; otherwise None (inherit) below the user level, or ``off``
    at it."""
    if value in MODES:
        return value
    return None if allow_inherit else "off"


def parse_categories(values: list[str]) -> str:
    """The known segment kinds among ``values``, in canonical order, for
    ``User.sponsorblock_categories``."""
    return ",".join(c for c in CATEGORIES if c in values)


def user_categories(user: User) -> list[str]:
    return [c for c in user.sponsorblock_categories.split(",") if c in CATEGORIES]


def strongest(modes: list[str | None]) -> str | None:
    """The strongest mode among those set (skip > show > off), for a channel
    in several categories: one that asks for SponsorBlock isn't overruled by
    another that doesn't."""
    present = [m for m in modes if m in MODES]
    return max(present, key=MODES.index) if present else None


def resolve_mode(db: Session, user: User, channel_id: str) -> str:
    """The mode for ``channel_id`` in ``user``'s player."""
    sub = db.execute(
        select(Subscription).where(Subscription.user_id == user.id, Subscription.channel_id == channel_id)
    ).scalar_one_or_none()
    cat_modes = (
        db.execute(
            select(Category.sponsorblock)
            .join(ChannelCategory, ChannelCategory.category_id == Category.id)
            .where(ChannelCategory.user_id == user.id, ChannelCategory.channel_id == channel_id)
        )
        .scalars()
        .all()
    )
    return resolve(sub.sponsorblock if sub else None, strongest(list(cat_modes)), user.sponsorblock)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    category: str


_cache: dict[str, tuple[float, list[Segment]]] = {}


def segments(video_id: str, categories: list[str], *, client: httpx.Client | None = None) -> list[Segment]:
    """``video_id``'s skippable segments of the given kinds, in order.

    Every kind is looked up and cached together, then filtered, so users with
    different choices share one lookup. Best-effort: SponsorBlock being down
    just means no segments.
    """
    now = time.monotonic()
    hit = _cache.get(video_id)
    if hit is None or hit[0] <= now:
        found = _fetch(video_id, client)
        _cache[video_id] = (now + (_TTL if found is not None else _ERROR_TTL), found or [])
        while len(_cache) > _CACHE_MAX:
            del _cache[next(iter(_cache))]  # oldest first: dicts keep insertion order
        hit = _cache[video_id]
    return [s for s in hit[1] if s.category in categories]


def _fetch(video_id: str, client: httpx.Client | None) -> list[Segment] | None:
    """All of ``video_id``'s skip segments, or None if SponsorBlock couldn't
    be reached."""
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    params = {"categories": json.dumps(list(CATEGORIES)), "actionTypes": json.dumps(["skip"])}
    try:
        resp = (client or httpx).get(f"{API_URL}/api/skipSegments/{prefix}", params=params, timeout=_TIMEOUT)
        if resp.status_code == 404:  # nothing submitted for any video with this prefix
            return []
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("SponsorBlock lookup for %s failed: %s", video_id, exc)
        return None
    for video in data:
        if video.get("videoID") == video_id:
            return sorted(
                (
                    Segment(start=float(s["segment"][0]), end=float(s["segment"][1]), category=s["category"])
                    for s in video.get("segments", [])
                    if s.get("actionType", "skip") == "skip"
                ),
                key=lambda s: s.start,
            )
    return []
