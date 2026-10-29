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

Retention is resolved separately for the two media kinds. ``keep_last_n``
governs the video; ``keep_last_n_audio`` governs the podcast audio, which at
~12x less per episode is usually worth keeping for much longer. The audio
window falls back to the video window *at each level* before climbing: a
channel you explicitly set to "keep 10" keeps 10 of both, rather than having a
global audio default quietly override the number you typed on that channel.
Nothing set anywhere means the two windows are identical, which is exactly the
behaviour before the column existed.

"Channel" above includes followed playlists: a subscription is to either, and
a playlist's videos are the ones seen on its feed (see services/sources). A
video can be in several followed sources, so a video's intent merges theirs.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from functools import reduce
from typing import TypeVar

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import (
    Category,
    Channel,
    ChannelCategory,
    Download,
    Subscription,
    User,
    Video,
    VideoShort,
)
from .prefs import resolve
from .sources import ARCHIVABLE_PLATFORMS, in_source, preferred, sources_of, video_sources
from .sponsorblock import PODCAST_MODES, strongest, user_categories

T = TypeVar("T")
U = TypeVar("U")

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
    #: 0 means no floor. Otherwise the *lowest* floor any subscriber asked for —
    #: permissive for a minimum means the smallest number, not the largest.
    min_duration_seconds: int
    #: True if at least one subscriber wants a podcast feed for it.
    generate_podcast: bool
    #: True if at least one *downloading* subscriber wants Shorts. When False,
    #: the channel excludes Shorts for everyone, so the worker skips them.
    include_shorts: bool
    #: True if at least one downloading subscriber has SponsorBlock on for it,
    #: so its segments get marked as chapters (never cut — see
    #: services/sponsorblock).
    sponsorblock: bool = False


def _known_shorts(db: Session, video_ids: list[str]) -> set[str]:
    """The subset of ``video_ids`` cached as Shorts. Read-only — an unclassified
    video is treated as non-Short (fail open), so nothing regular is dropped."""
    if not video_ids:
        return set()
    rows = (
        db.execute(select(VideoShort.video_id).where(VideoShort.video_id.in_(video_ids), VideoShort.is_short.is_(True)))
        .scalars()
        .all()
    )
    return set(rows)


def _most_permissive_int(values: list[int | None]) -> int | None:
    """Largest value, where 0 (unlimited) beats everything."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    if any(v == 0 for v in present):
        return 0
    return max(present)


def _least_restrictive_floor(values: list[int | None]) -> int | None:
    """Smallest value — the mirror of ``_most_permissive_int`` for a floor.

    Permissive means letting *more* through, so for a minimum duration that's
    the lowest bar rather than the highest. 0 (no floor) needs no special case
    here: it is already the smallest value there is.
    """
    present = [v for v in values if v is not None]
    return min(present) if present else None


@dataclass(frozen=True)
class _CategoryPrefs:
    """The most permissive value for each pref across a channel's categories.

    ``None`` in any field means no category expressed an opinion, so the user
    default decides. A record rather than a tuple because six positional
    ``None``s at two call sites is a transposition waiting to happen.
    """

    download: bool | None = None
    keep: int | None = None
    keep_audio: int | None = None
    max_duration: int | None = None
    min_duration: int | None = None
    podcast: bool | None = None
    shorts: bool | None = None
    sponsorblock: str | None = None
    podcast_sponsorblock: str | None = None


def _category_prefs(db: Session, user_id: int, channel_id: str) -> _CategoryPrefs:
    """Most permissive preference across every category this channel is in."""
    categories = (
        db.execute(
            select(Category)
            .join(ChannelCategory, ChannelCategory.category_id == Category.id)
            .where(
                ChannelCategory.user_id == user_id,
                ChannelCategory.channel_id == channel_id,
            )
        )
        .scalars()
        .all()
    )
    if not categories:
        return _CategoryPrefs()

    downloads = [c.download_enabled for c in categories if c.download_enabled is not None]
    podcasts = [c.generate_podcast for c in categories if c.generate_podcast is not None]
    shorts = [c.include_shorts for c in categories if c.include_shorts is not None]
    return _CategoryPrefs(
        download=any(downloads) if downloads else None,
        keep=_most_permissive_int([c.keep_last_n for c in categories]),
        # Each category's audio window falls back to its own video window before
        # they're compared, so a category that only sets keep_last_n still gets
        # a say in the audio answer.
        keep_audio=_most_permissive_int([_or(c.keep_last_n_audio, c.keep_last_n) for c in categories]),
        max_duration=_most_permissive_int([c.max_duration_seconds for c in categories]),
        min_duration=_least_restrictive_floor([c.min_duration_seconds for c in categories]),
        podcast=any(podcasts) if podcasts else None,
        shorts=any(shorts) if shorts else None,
        sponsorblock=strongest([c.sponsorblock for c in categories]),
        podcast_sponsorblock=strongest([c.podcast_sponsorblock for c in categories], PODCAST_MODES),
    )


def _archivable_subscriptions(db: Session) -> list[tuple[Subscription, str]]:
    """Non-ignored subscriptions to channels or playlists that can be
    downloaded at all, each with its platform.

    A Nebula channel sitting in a category (or under an account default) with
    downloads on must not produce an intent — there's nothing the downloader
    could fetch for it.
    """
    return list(
        db.execute(
            select(Subscription, Channel.platform)
            .join(Channel, Channel.channel_id == Subscription.channel_id)
            .where(
                Subscription.ignored == False,  # noqa: E712
                Channel.platform.in_(ARCHIVABLE_PLATFORMS),
            )
        )
        .tuples()
        .all()
    )


def _or(value: T | None, fallback: U) -> T | U:
    """``value`` unless it's NULL (inherit), in which case ``fallback``."""
    return fallback if value is None else value


def _audio_keep(sub, cat_prefs: _CategoryPrefs, user) -> int:
    """The retention window for this subscription's podcast audio.

    The audio column wins at whichever level speaks first, falling back to that
    same level's video window — so specificity beats media kind. Setting
    ``keep_last_n_audio`` nowhere leaves this identical to ``keep_last_n``.
    """
    return resolve(
        _or(sub.keep_last_n_audio, sub.keep_last_n),
        cat_prefs.keep_audio,
        _or(user.keep_last_n_audio, user.keep_last_n),
    )


def channel_intents(db: Session) -> dict[str, ChannelIntent]:
    """Resolve every subscribed channel and playlist into a single global
    intent, keyed by its id."""
    subs = _archivable_subscriptions(db)

    users = {u.id: u for u in db.execute(select(User)).scalars().all()}
    intents: dict[str, ChannelIntent] = {}

    for sub, _ in subs:
        user = users.get(sub.user_id)
        if user is None:
            continue

        cat = _category_prefs(db, sub.user_id, sub.channel_id)

        download = resolve(sub.download_enabled, cat.download, user.download_enabled)
        keep = resolve(sub.keep_last_n, cat.keep, user.keep_last_n)
        max_dur = resolve(sub.max_duration_seconds, cat.max_duration, user.max_duration_seconds)
        min_dur = resolve(sub.min_duration_seconds, cat.min_duration, user.min_duration_seconds)
        podcast = resolve(sub.generate_podcast, cat.podcast, user.generate_podcast)
        # Only a subscriber who actually downloads this channel gets a say in
        # whether its Shorts are wanted on disk.
        wants_shorts = download and resolve(sub.include_shorts, cat.shorts, user.include_shorts)
        wants_sponsorblock = download and resolve(sub.sponsorblock, cat.sponsorblock, user.sponsorblock) != "off"

        intent = ChannelIntent(
            channel_id=sub.channel_id,
            download=download,
            keep_last_n=keep,
            max_duration_seconds=max_dur,
            min_duration_seconds=min_dur,
            generate_podcast=podcast,
            include_shorts=wants_shorts,
            sponsorblock=wants_sponsorblock,
        )
        existing = intents.get(sub.channel_id)
        intents[sub.channel_id] = intent if existing is None else _merge(existing, intent)

    return intents


def _merge(a: ChannelIntent, b: ChannelIntent) -> ChannelIntent:
    """Two intents as one, most permissive in every field. Keeps ``a``'s id."""
    return ChannelIntent(
        channel_id=a.channel_id,
        download=a.download or b.download,
        keep_last_n=_most_permissive_int([a.keep_last_n, b.keep_last_n]) or 0,
        max_duration_seconds=_most_permissive_int([a.max_duration_seconds, b.max_duration_seconds]) or 0,
        min_duration_seconds=_least_restrictive_floor([a.min_duration_seconds, b.min_duration_seconds]) or 0,
        generate_podcast=a.generate_podcast or b.generate_podcast,
        include_shorts=a.include_shorts or b.include_shorts,
        sponsorblock=a.sponsorblock or b.sponsorblock,
    )


def video_intent(db: Session, video_id: str) -> ChannelIntent | None:
    """What subscribers want for one video: the intents of its channel and of
    every playlist it's in, merged, or None if nobody follows any of them."""
    intents = channel_intents(db)
    vs = video_sources()
    found = [
        intents[source_id]
        for source_id in db.execute(select(vs.c.source_id).where(vs.c.video_id == video_id)).scalars()
        if source_id in intents
    ]
    return reduce(_merge, found) if found else None


def _archivable_videos(source_id: str, platform: str):
    """A condition on ``Video``: the ones a subscription to this source can
    archive. For a channel, not the older uploads found other than on its
    feed (``backfilled``): those are for search, and would otherwise queue a
    "keep everything" channel's whole catalogue. A playlist's are all from
    its feed already."""
    if platform == "youtube":
        return and_(in_source(source_id, platform), Video.backfilled.is_(False))
    return in_source(source_id, platform)


def _retained_sources(db: Session, *, podcast: bool) -> dict[int, dict[str, str]]:
    """Shared core of the per-user video/podcast retention sets: per user, the
    videos they retain, each with the source it's filed under.

    ``podcast=False`` gates on ``download_enabled`` — the videos whose file is
    kept on disk and shown in the user's Jellyfin library. ``podcast=True`` gates
    on ``generate_podcast`` — the videos whose extracted audio is kept for the
    user's podcast feed. Each applies its own retention window (``keep_last_n``
    for video, ``keep_last_n_audio`` falling back to it for audio) with identical
    Shorts filtering. One function so the two media kinds can't drift apart in
    how a window is applied, only in how wide it is.

    All three preferences resolve via the cascade (subscription >
    most-permissive category > user). A user who wants neither contributes
    nothing — their ``keep_last_n`` never inflates anyone else's retention.
    A video one user retains through more than one source (a channel and a
    playlist of it, say) is filed under ``sources.preferred``.
    """
    subs = _archivable_subscriptions(db)
    users = {u.id: u for u in db.execute(select(User)).scalars().all()}

    found: dict[int, dict[str, list[tuple[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for sub, platform in subs:
        user = users.get(sub.user_id)
        if user is None:
            continue

        cat = _category_prefs(db, sub.user_id, sub.channel_id)
        if podcast:
            wanted = resolve(sub.generate_podcast, cat.podcast, user.generate_podcast)
            keep = _audio_keep(sub, cat, user)
        else:
            wanted = resolve(sub.download_enabled, cat.download, user.download_enabled)
            keep = resolve(sub.keep_last_n, cat.keep, user.keep_last_n)
        if not wanted:
            continue
        include_shorts = resolve(sub.include_shorts, cat.shorts, user.include_shorts)

        stmt = (
            select(Video.video_id)
            .where(_archivable_videos(sub.channel_id, platform))
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

        for vid in vids:
            found[sub.user_id][vid].append((sub.channel_id, platform))

    return {
        user_id: {vid: preferred(sources) for vid, sources in by_video.items()} for user_id, by_video in found.items()
    }


def user_retained_video_sources(db: Session) -> dict[int, dict[str, str]]:
    """``user_retained_video_ids`` with the source each video is filed under
    — the series it appears as in that user's library."""
    return _retained_sources(db, podcast=False)


def user_retained_video_ids(db: Session) -> dict[int, set[str]]:
    """Per user, the videos that should be visible in *their* Jellyfin library.

    This is the per-user layer that decides hardlink placement, distinct from
    the global union (below) that decides whether a file exists on disk at all.
    Gated on ``download_enabled``: a channel a user wants only as a podcast
    contributes nothing here, so its video is never linked into their library.
    """
    return {user_id: set(vids) for user_id, vids in _retained_sources(db, podcast=False).items()}


def user_retained_podcast_ids(db: Session) -> dict[int, set[str]]:
    """Per user, the videos whose audio their podcast feed should keep.

    The audio analogue of ``user_retained_video_ids``, gated on
    ``generate_podcast`` instead of ``download_enabled`` and windowed by
    ``keep_last_n_audio``. This is what lets a channel be wanted as a podcast
    without keeping the full video: the id lands here (so the audio is fetched
    and retained) but not in the video set (so no ``.mkv`` is kept and it never
    enters Jellyfin). A wider audio window than video window puts an id here
    only — the same mechanism, so old episodes keep their audio while the video
    is pruned on the shorter schedule.
    """
    return {user_id: set(vids) for user_id, vids in _retained_sources(db, podcast=True).items()}


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


def wanted_audio_cuts(db: Session) -> dict[str, set[str]]:
    """Per video whose podcast audio is kept, the SponsorBlock cuts its
    subscribers want made: one set of segment kinds (as stored in
    ``AudioCut.categories``) per distinct choice among those who have
    ``podcast_sponsorblock`` resolve to ``remove`` for any channel or
    playlist of theirs the video is in.

    The per-user resolution matches ``sponsorblock.podcast_cut_for``, which
    is what each user's feed looks the cut up by.
    """
    retained = user_retained_podcast_ids(db)
    if not retained:
        return {}
    users = {u.id: u for u in db.execute(select(User)).scalars().all()}

    kinds_by_user: dict[int, str] = {}
    cut_sources: set[tuple[int, str]] = set()  # (user, channel or playlist)
    for sub, _ in _archivable_subscriptions(db):
        user = users.get(sub.user_id)
        if user is None or sub.user_id not in retained:
            continue
        cat = _category_prefs(db, sub.user_id, sub.channel_id)
        mode = resolve(sub.podcast_sponsorblock, cat.podcast_sponsorblock, user.podcast_sponsorblock)
        kinds = ",".join(user_categories(user, podcast=True))
        if mode == "remove" and kinds:
            cut_sources.add((sub.user_id, sub.channel_id))
            kinds_by_user[sub.user_id] = kinds
    if not cut_sources:
        return {}

    sources = sources_of(db, set().union(*retained.values()))
    wanted: dict[str, set[str]] = {}
    for user_id, vids in retained.items():
        for vid in vids:
            if any((user_id, source_id) in cut_sources for source_id in sources[vid]):
                wanted.setdefault(vid, set()).add(kinds_by_user[user_id])
    return wanted


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

    existing = set(db.execute(select(Download.video_id).where(Download.video_id.in_(wanted))).scalars().all())

    created = 0
    for video_id in wanted - existing:
        db.add(Download(video_id=video_id, status="pending"))
        created += 1
    if created:
        logger.info("Enqueued %d new downloads", created)
    return created
