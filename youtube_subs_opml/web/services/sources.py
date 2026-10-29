"""Which followed channels and playlists a video is in.

A video belongs to the channel that uploaded it (``Video.channel_id``), and is
also in every playlist it's been seen on (``playlist_videos``). Either can be
followed — both are rows in ``subscriptions`` — so anything asking "whose is
this video?" or "which videos does this subscription cover?" goes through here
rather than reading ``Video.channel_id`` directly. "Source" below means a
followed channel or playlist: a ``subscriptions.channel_id``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import Select, and_, select, union_all
from sqlalchemy.orm import Session

from youtube_subs_opml import playlists

from ..models import PlaylistVideo, Subscription, Video

# Platforms whose videos can be downloaded. Nebula's can't.
ARCHIVABLE_PLATFORMS = ("youtube", playlists.PLATFORM)


def video_sources():
    """A subquery of ``(video_id, source_id)``: each video with its channel,
    and with each playlist it's in."""
    return union_all(
        select(Video.video_id.label("video_id"), Video.channel_id.label("source_id")),
        select(PlaylistVideo.video_id.label("video_id"), PlaylistVideo.playlist_id.label("source_id")),
    ).subquery("video_sources")


def sources_of(db: Session, video_ids: Iterable[str]) -> dict[str, set[str]]:
    """The channel and playlists each of ``video_ids`` is in, followed or not."""
    vs = video_sources()
    found: dict[str, set[str]] = defaultdict(set)
    for video_id, source_id in db.execute(
        select(vs.c.video_id, vs.c.source_id).where(vs.c.video_id.in_(list(video_ids)))
    ).tuples():
        found[video_id].add(source_id)
    return found


def source_video_ids(source_id: str) -> Select:
    """The ids of the videos in one channel or playlist, for ``in_()`` filters."""
    vs = video_sources()
    return select(vs.c.video_id).where(vs.c.source_id == source_id)


def followed_video_ids(user_id: int, *, include_ignored: bool = False) -> Select:
    """The ids of every video in a channel or playlist ``user_id`` follows,
    each once, for ``Video.video_id.in_(...)`` filters. Ignored subscriptions
    are left out unless ``include_ignored``."""
    vs = video_sources()
    on = and_(Subscription.channel_id == vs.c.source_id, Subscription.user_id == user_id)
    if not include_ignored:
        on = and_(on, Subscription.ignored.is_(False))
    return select(vs.c.video_id).join(Subscription, on).distinct()


def in_source(source_id: str, platform: str):
    """A condition on ``Video``: in this channel or playlist."""
    if platform == playlists.PLATFORM:
        return Video.video_id.in_(select(PlaylistVideo.video_id).where(PlaylistVideo.playlist_id == source_id))
    return Video.channel_id == source_id


def preferred(sources: Iterable[tuple[str, str]]) -> str:
    """The one of a video's ``(source_id, platform)`` sources to file it
    under: its channel if that's among them, otherwise the first playlist by
    id, so the choice is stable from one pass to the next."""
    ranked = sorted(sources, key=lambda s: (s[1] == playlists.PLATFORM, s[0]))
    return ranked[0][0]
