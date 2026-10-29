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

Podcast feeds have their own preference, cascading the same way: ``off``, or
``remove``, which cuts the segments out of the episode's audio. Which kinds
of segment it cuts is again a user-level choice, separate from the player's —
so a sponsor read can be marked in the video and cut from the podcast. The
extracted audio is shared like the video, so cutting never touches it: the
downloader makes a cut copy per distinct choice of kinds (see
``downloader/sponsor_cuts``) and each subscriber's feed serves theirs.

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
from collections.abc import Iterable
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
#: For podcast audio, weakest first like ``MODES``.
PODCAST_MODES: tuple[str, ...] = ("off", "remove")

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
def parse_mode(value: str | None, *, allow_inherit: Literal[False], modes: tuple[str, ...] = MODES) -> str: ...


@overload
def parse_mode(value: str | None, *, allow_inherit: bool, modes: tuple[str, ...] = MODES) -> str | None: ...


def parse_mode(value: str | None, *, allow_inherit: bool, modes: tuple[str, ...] = MODES) -> str | None:
    """One of ``modes`` (the player's, unless given ``PODCAST_MODES``);
    otherwise None (inherit) below the user level, or ``off`` at it."""
    if value in modes:
        return value
    return None if allow_inherit else "off"


def parse_categories(values: list[str]) -> str:
    """The known segment kinds among ``values``, in canonical order, for
    ``User.sponsorblock_categories``."""
    return ",".join(c for c in CATEGORIES if c in values)


def user_categories(user: User, *, podcast: bool = False) -> list[str]:
    """The kinds ``user`` has chosen for the player, or for podcasts."""
    chosen = user.podcast_sponsorblock_categories if podcast else user.sponsorblock_categories
    return [c for c in chosen.split(",") if c in CATEGORIES]


def strongest(modes: list[str | None], order: tuple[str, ...] = MODES) -> str | None:
    """The strongest mode among those set (skip > show > off, or remove >
    off), for a channel in several categories: one that asks for SponsorBlock
    isn't overruled by another that doesn't."""
    present = [m for m in modes if m in order]
    return max(present, key=order.index) if present else None


def _resolve(db: Session, user: User, channel_id: str, field: str, order: tuple[str, ...]) -> str:
    sub = db.execute(
        select(Subscription).where(Subscription.user_id == user.id, Subscription.channel_id == channel_id)
    ).scalar_one_or_none()
    cat_modes = (
        db.execute(
            select(getattr(Category, field))
            .join(ChannelCategory, ChannelCategory.category_id == Category.id)
            .where(ChannelCategory.user_id == user.id, ChannelCategory.channel_id == channel_id)
        )
        .scalars()
        .all()
    )
    return resolve(getattr(sub, field) if sub else None, strongest(list(cat_modes), order), getattr(user, field))


def resolve_mode(db: Session, user: User, channel_id: str) -> str:
    """The mode for ``channel_id`` in ``user``'s player."""
    return _resolve(db, user, channel_id, "sponsorblock", MODES)


def resolve_podcast_mode(db: Session, user: User, channel_id: str) -> str:
    """The mode for ``channel_id``'s episodes in ``user``'s podcast feeds."""
    return _resolve(db, user, channel_id, "podcast_sponsorblock", PODCAST_MODES)


def podcast_cut(db: Session, user: User, channel_id: str) -> str | None:
    """The kinds cut from ``channel_id``'s episodes for ``user``, as stored
    in ``AudioCut.categories``; None when nothing is."""
    if resolve_podcast_mode(db, user, channel_id) != "remove":
        return None
    return ",".join(user_categories(user, podcast=True)) or None


def podcast_cut_for(db: Session, user: User, source_ids: Iterable[str]) -> str | None:
    """``podcast_cut`` for a video in several channels and playlists (its
    ``source_ids``): the cut if any of them that ``user`` follows, and isn't
    ignoring, asks for one."""
    followed = db.execute(
        select(Subscription.channel_id).where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_(list(source_ids)),
            Subscription.ignored.is_(False),
        )
    ).scalars()
    return next((kinds for channel_id in followed if (kinds := podcast_cut(db, user, channel_id))), None)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    category: str


_cache: dict[str, tuple[float, list[Segment] | None]] = {}


def segments(video_id: str, categories: list[str], *, client: httpx.Client | None = None) -> list[Segment]:
    """``video_id``'s skippable segments of the given kinds, in order.

    Best-effort: SponsorBlock being down just means no segments.
    """
    return [s for s in lookup(video_id, client=client) or [] if s.category in categories]


def lookup(video_id: str, *, client: httpx.Client | None = None) -> list[Segment] | None:
    """All of ``video_id``'s skippable segments, in order, or None if
    SponsorBlock couldn't be reached.

    Every kind is looked up and cached together, so users with different
    choices share one lookup.
    """
    now = time.monotonic()
    hit = _cache.get(video_id)
    if hit is None or hit[0] <= now:
        found = _fetch(video_id, client)
        _cache[video_id] = (now + (_TTL if found is not None else _ERROR_TTL), found)
        while len(_cache) > _CACHE_MAX:
            del _cache[next(iter(_cache))]  # oldest first: dicts keep insertion order
        hit = _cache[video_id]
    return hit[1]


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
