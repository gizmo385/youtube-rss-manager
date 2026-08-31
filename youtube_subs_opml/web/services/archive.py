"""Turning per-user preferences into global download and prune decisions.

The awkwardness this module exists to absorb: preferences are per-user, but the
file on disk is shared. If two people subscribe to the same channel and one
sets ``keep_last_n=10`` while the other sets ``20``, there is one file tree and
one answer.

The rule is **most permissive wins**. A video is downloaded if *any* user wants
it, and pruned only when *no* user wants to retain it. That means a housemate
tightening their retention never deletes someone else's videos, which is the
failure mode worth avoiding at household scale.

A second ambiguity: a subscription can belong to several categories, which may
disagree. Resolution here is subscription-level value first if set, otherwise
the most permissive value across the categories it belongs to, otherwise the
user default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Category,
    ChannelCategory,
    Download,
    Subscription,
    User,
    Video,
    VideoShort,
)
from .prefs import resolve

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelIntent:
    """The union of every subscriber's wishes for one channel."""

    channel_id: str
    #: True if at least one subscriber wants this channel archived.
    download: bool
    #: 0 means unlimited. Otherwise the largest N any subscriber asked for.
    keep_last_n: int
    #: 0 means no limit. Otherwise the largest cap any subscriber asked for.
    max_duration_seconds: int
    #: True if at least one subscriber wants a podcast feed for it.
    generate_podcast: bool
    #: True if at least one *downloading* subscriber wants Shorts. When False,
    #: the channel excludes Shorts for everyone, so the worker skips them.
    include_shorts: bool


def _known_shorts(db: Session, video_ids: list[str]) -> set[str]:
    """The subset of ``video_ids`` cached as Shorts. Read-only — an unclassified
    video is treated as non-Short (fail open), so nothing regular is dropped."""
    if not video_ids:
        return set()
    rows = db.execute(
        select(VideoShort.video_id).where(
            VideoShort.video_id.in_(video_ids), VideoShort.is_short.is_(True)
        )
    ).scalars().all()
    return set(rows)


def _most_permissive_int(values: list[int | None]) -> int | None:
    """Largest value, where 0 (unlimited) beats everything."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    if any(v == 0 for v in present):
        return 0
    return max(present)


def _category_prefs(
    db: Session, user_id: int, channel_id: str
) -> tuple[bool | None, int | None, int | None, bool | None, bool | None]:
    """Most permissive preference across every category this channel is in."""
    categories = db.execute(
        select(Category)
        .join(ChannelCategory, ChannelCategory.category_id == Category.id)
        .where(
            ChannelCategory.user_id == user_id,
            ChannelCategory.channel_id == channel_id,
        )
    ).scalars().all()
    if not categories:
        return None, None, None, None, None

    downloads = [c.download_enabled for c in categories if c.download_enabled is not None]
    podcasts = [c.generate_podcast for c in categories if c.generate_podcast is not None]
    shorts = [c.include_shorts for c in categories if c.include_shorts is not None]
    return (
        (True if any(downloads) else False) if downloads else None,
        _most_permissive_int([c.keep_last_n for c in categories]),
        _most_permissive_int([c.max_duration_seconds for c in categories]),
        (True if any(podcasts) else False) if podcasts else None,
        (True if any(shorts) else False) if shorts else None,
    )


def channel_intents(db: Session) -> dict[str, ChannelIntent]:
    """Resolve every subscribed channel into a single global intent."""
    subs = db.execute(
        select(Subscription).where(Subscription.ignored == False)  # noqa: E712
    ).scalars().all()

    users = {u.id: u for u in db.execute(select(User)).scalars().all()}
    intents: dict[str, ChannelIntent] = {}

    for sub in subs:
        user = users.get(sub.user_id)
        if user is None:
            continue

        cat_download, cat_keep, cat_max, cat_podcast, cat_shorts = _category_prefs(
            db, sub.user_id, sub.channel_id
        )

        download = resolve(sub.download_enabled, cat_download, user.download_enabled)
        keep = resolve(sub.keep_last_n, cat_keep, user.keep_last_n)
        max_dur = resolve(
            sub.max_duration_seconds, cat_max, user.max_duration_seconds
        )
        podcast = resolve(sub.generate_podcast, cat_podcast, user.generate_podcast)
        # Only a subscriber who actually downloads this channel gets a say in
        # whether its Shorts are wanted on disk.
        wants_shorts = download and resolve(
            sub.include_shorts, cat_shorts, user.include_shorts
        )

        existing = intents.get(sub.channel_id)
        if existing is None:
            intents[sub.channel_id] = ChannelIntent(
                channel_id=sub.channel_id,
                download=download,
                keep_last_n=keep,
                max_duration_seconds=max_dur,
                generate_podcast=podcast,
                include_shorts=wants_shorts,
            )
            continue

        # Merge: most permissive across subscribers.
        intents[sub.channel_id] = ChannelIntent(
            channel_id=sub.channel_id,
            download=existing.download or download,
            keep_last_n=_most_permissive_int([existing.keep_last_n, keep]) or 0,
            max_duration_seconds=_most_permissive_int(
                [existing.max_duration_seconds, max_dur]
            )
            or 0,
            generate_podcast=existing.generate_podcast or podcast,
            include_shorts=existing.include_shorts or wants_shorts,
        )

    return intents


def _retained_ids(db: Session, *, podcast: bool) -> dict[int, set[str]]:
    """Shared core of the per-user video/podcast retention sets.

    ``podcast=False`` gates on ``download_enabled`` — the videos whose file is
    kept on disk and shown in the user's Jellyfin library. ``podcast=True`` gates
    on ``generate_podcast`` — the videos whose extracted audio is kept for the
    user's podcast feed. Both apply the same ``keep_last_n`` window and Shorts
    filtering; the only difference is which preference decides the user wants
    this channel at all. One function so the window and Shorts handling can't
    drift apart between the two media kinds.

    All three preferences resolve via the cascade (subscription >
    most-permissive category > user). A user who wants neither contributes
    nothing — their ``keep_last_n`` never inflates anyone else's retention.
    """
    subs = db.execute(
        select(Subscription).where(Subscription.ignored == False)  # noqa: E712
    ).scalars().all()
    users = {u.id: u for u in db.execute(select(User)).scalars().all()}

    retained: dict[int, set[str]] = {}
    for sub in subs:
        user = users.get(sub.user_id)
        if user is None:
            continue

        cat_download, cat_keep, _cat_max, cat_podcast, cat_shorts = _category_prefs(
            db, sub.user_id, sub.channel_id
        )
        if podcast:
            wanted = resolve(sub.generate_podcast, cat_podcast, user.generate_podcast)
        else:
            wanted = resolve(sub.download_enabled, cat_download, user.download_enabled)
        if not wanted:
            continue
        keep = resolve(sub.keep_last_n, cat_keep, user.keep_last_n)
        include_shorts = resolve(sub.include_shorts, cat_shorts, user.include_shorts)

        stmt = (
            select(Video.video_id)
            .where(Video.channel_id == sub.channel_id)
            .order_by(Video.published_at.desc().nullslast())
        )
        if include_shorts:
            # No Shorts filtering, so the DB can apply the keep window directly.
            if keep > 0:
                stmt = stmt.limit(keep)
            vids = list(db.execute(stmt).scalars().all())
        else:
            # Drop known Shorts before applying keep, so a burst of Shorts can't
            # push the wanted regular uploads out of the retention window.
            candidates = list(db.execute(stmt).scalars().all())
            shorts = _known_shorts(db, candidates)
            vids = [v for v in candidates if v not in shorts]
            if keep > 0:
                vids = vids[:keep]

        retained.setdefault(sub.user_id, set()).update(vids)
    return retained


def user_retained_video_ids(db: Session) -> dict[int, set[str]]:
    """Per user, the videos that should be visible in *their* Jellyfin library.

    This is the per-user layer that decides hardlink placement, distinct from
    the global union (below) that decides whether a file exists on disk at all.
    Gated on ``download_enabled``: a channel a user wants only as a podcast
    contributes nothing here, so its video is never linked into their library.
    """
    return _retained_ids(db, podcast=False)


def user_retained_podcast_ids(db: Session) -> dict[int, set[str]]:
    """Per user, the videos whose audio their podcast feed should keep.

    The audio analogue of ``user_retained_video_ids``, gated on
    ``generate_podcast`` instead of ``download_enabled``. This is what lets a
    channel be wanted as a podcast without keeping the full video: the id lands
    here (so the audio is fetched and retained) but not in the video set (so no
    ``.mkv`` is kept and it never enters Jellyfin).
    """
    return _retained_ids(db, podcast=True)


def retained_video_ids(db: Session) -> set[str]:
    """Every video id that should exist on disk right now.

    The global "keep on disk" set is exactly the union of what each user
    individually retains: a file survives as long as *any* user wants it, and is
    prunable only when *no* user does. Building it from the per-user sets keeps
    a single source of truth — the canonical file and the per-user hardlinks
    can't disagree about what should exist.
    """
    keep: set[str] = set()
    for ids in user_retained_video_ids(db).values():
        keep.update(ids)
    return keep


def retained_podcast_ids(db: Session) -> set[str]:
    """Every video id whose *audio* should exist on disk right now (union).

    The podcast analogue of ``retained_video_ids``: the audio survives as long
    as any user wants that channel as a podcast, independent of whether anyone
    keeps the video.
    """
    keep: set[str] = set()
    for ids in user_retained_podcast_ids(db).values():
        keep.update(ids)
    return keep


def enqueue_pending(db: Session) -> int:
    """Create ``pending`` Download rows for wanted videos that lack one.

    "Wanted" is the union of the video and podcast retention sets, so a channel
    wanted only as a podcast still gets enqueued — the worker fetches its audio
    without the video. Duration is not consulted here — the poller doesn't know
    it. The worker probes metadata first and marks the row ``skipped``/
    ``too_long`` if it exceeds the cap, which avoids fetching a byte of media
    for a six-hour stream.
    """
    wanted = retained_video_ids(db) | retained_podcast_ids(db)
    if not wanted:
        return 0

    existing = set(
        db.execute(
            select(Download.video_id).where(Download.video_id.in_(wanted))
        ).scalars().all()
    )

    created = 0
    for video_id in wanted - existing:
        db.add(Download(video_id=video_id, status="pending"))
        created += 1
    if created:
        logger.info("Enqueued %d new downloads", created)
    return created


