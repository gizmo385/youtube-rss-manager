"""The Watch tab's feed: recent uploads across the user's subscriptions,
grouped by category, with per-user watched state.

Built entirely from what the poller has already stored — ``videos`` for YouTube
and the cached RSS for Nebula (whose videos never land in ``videos``) — so
rendering the page never reaches out to either service. Shorts and
live/upcoming filtering likewise only consult the existing caches: a video
nobody has classified yet is shown rather than probed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from youtube_subs_opml import nebula

from ..models import (
    Category,
    Channel,
    ChannelCategory,
    ChannelFeedCache,
    Download,
    DownloadLink,
    JellyfinAccount,
    Subscription,
    User,
    Video,
    VideoLiveStatus,
    VideoShort,
    WatchedVideo,
)
from .jellyfin import jellyfin_deep_link
from .live import resolve_include_live
from .shorts import resolve_include_shorts

# How far back the feed reaches. Older uploads drop off rather than piling up
# as an ever-growing unwatched backlog.
WINDOW = timedelta(days=30)

UNCATEGORIZED = "unc"

# Nebula's RSS carries no thumbnail element; the poster is the first image in
# the item's HTML description.
_NEBULA_IMG = re.compile(r"https://images\.nebula\.tv/[0-9a-f-]+")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


def _nebula_videos(channel_id: str, xml: bytes, since: datetime | None) -> list[dict]:
    """Items from a cached Nebula feed, keyed ``nebula:{video slug}``."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for item in root.iterfind("channel/item"):
        link = (item.findtext("link") or "").strip()
        slug = link.rstrip("/").rsplit("/", 1)[-1] if "/videos/" in link else ""
        if not slug:
            continue
        try:
            published = _aware(parsedate_to_datetime(item.findtext("pubDate") or ""))
        except (TypeError, ValueError):
            continue
        if since is not None and published < since:
            continue
        img = _NEBULA_IMG.search(item.findtext("description") or "")
        out.append(
            {
                "key": f"{nebula.ID_PREFIX}{slug}",
                "title": item.findtext("title") or slug,
                "channel_id": channel_id,
                "published": published,
                "duration": None,
                "url": link,
                "thumb": f"{img.group(0)}?format=jpeg&width=640" if img else None,
                "video_id": None,
            }
        )
    return out


def _load_videos(
    db: Session, channels: dict[str, Channel], since: datetime | None, limit: int | None = None
) -> list[dict]:
    """Stored videos for ``channels``, newest first: YouTube from ``videos``,
    Nebula from the cached feed. ``since`` bounds by age; ``limit`` caps the
    YouTube query (a Nebula feed is already only its recent items)."""
    youtube_ids = [cid for cid, ch in channels.items() if ch.platform != nebula.PLATFORM]
    nebula_ids = [cid for cid, ch in channels.items() if ch.platform == nebula.PLATFORM]

    videos: list[dict] = []
    if youtube_ids:
        stmt = (
            select(Video)
            .where(Video.channel_id.in_(youtube_ids), Video.published_at.is_not(None))
            .order_by(Video.published_at.desc())
        )
        if since is not None:
            stmt = stmt.where(Video.published_at >= since)
        if limit is not None:
            stmt = stmt.limit(limit)
        for v in db.execute(stmt).scalars().all():
            published = v.published_at
            assert published is not None  # filtered above; narrows for the type checker
            videos.append(
                {
                    "key": v.video_id,
                    "title": v.title or v.video_id,
                    "channel_id": v.channel_id,
                    "published": _aware(published),
                    "duration": v.duration_seconds,
                    "url": f"https://www.youtube.com/watch?v={v.video_id}",
                    "thumb": f"https://i.ytimg.com/vi/{v.video_id}/mqdefault.jpg",
                    "video_id": v.video_id,
                }
            )
    if nebula_ids:
        for channel_id, xml in db.execute(
            select(ChannelFeedCache.channel_id, ChannelFeedCache.xml).where(ChannelFeedCache.channel_id.in_(nebula_ids))
        ).all():
            videos.extend(_nebula_videos(channel_id, xml, since))
    videos.sort(key=lambda v: v["published"], reverse=True)
    return videos


def _annotate(user: User, db: Session, videos: list[dict]) -> None:
    """Add per-user state in place: ``archived``, ``watched``, and — for
    archived copies in the user's Jellyfin library — ``jellyfin`` with ``url``
    pointing there instead of YouTube."""
    yt_video_ids = [v["video_id"] for v in videos if v["video_id"]]

    archived: set[str] = set()
    items: dict[str, str] = {}
    if yt_video_ids:
        archived = set(
            db.execute(
                select(Download.video_id).where(Download.video_id.in_(yt_video_ids), Download.status == "complete")
            )
            .scalars()
            .all()
        )
    # Archived copies open in Jellyfin instead — but only once this user's
    # library has the item, or there'd be nowhere to send them.
    account = db.execute(select(JellyfinAccount).where(JellyfinAccount.user_id == user.id)).scalar_one_or_none()
    if account and account.base_url and yt_video_ids:
        items = {
            vid: item
            for vid, item in db.execute(
                select(DownloadLink.video_id, DownloadLink.jellyfin_item_id)
                .join(Download, Download.video_id == DownloadLink.video_id)
                .where(
                    DownloadLink.user_id == user.id,
                    DownloadLink.video_id.in_(yt_video_ids),
                    DownloadLink.jellyfin_item_id.is_not(None),
                    Download.status == "complete",
                )
            ).all()
            if item
        }

    keys = [v["key"] for v in videos]
    watched: set[str] = set()
    if keys:
        watched = set(
            db.execute(
                select(WatchedVideo.video_key).where(WatchedVideo.user_id == user.id, WatchedVideo.video_key.in_(keys))
            )
            .scalars()
            .all()
        )

    for v in videos:
        item_id = items.get(v["video_id"])
        v["jellyfin"] = bool(item_id)
        if item_id and account:
            v["url"] = jellyfin_deep_link(account.base_url, item_id)
        v["archived"] = v["video_id"] in archived
        v["watched"] = v["key"] in watched


def _client_video(v: dict, channel: Channel) -> dict:
    """The JSON shape static/watch.js renders."""
    return {
        "key": v["key"],
        "title": v["title"],
        "channel": channel.title or channel.channel_id,
        "channelId": channel.channel_id,
        "nebula": channel.platform == nebula.PLATFORM,
        "jellyfin": v["jellyfin"],
        "archived": v["archived"],
        "videoId": v["video_id"],
        "published": v["published"].isoformat(),
        "duration": v["duration"],
        "url": v["url"],
        "thumb": v["thumb"],
        "watched": v["watched"],
    }


def build_watch_feed(user: User, db: Session, now: datetime | None = None) -> dict:
    """Everything the Watch page renders, as JSON-ready data.

    Returns ``categories`` (in display order, Uncategorized last, each with the
    channels that have something in the window) and ``videos`` (newest first,
    each listing the category ids it appears under). A channel in two
    categories puts its videos in both; the client keys cards by category and
    video so the duplicates stay distinct.
    """
    now = now or datetime.now(UTC)

    subs = db.execute(
        select(Subscription, Channel)
        .join(Channel, Channel.channel_id == Subscription.channel_id)
        .where(Subscription.user_id == user.id, Subscription.ignored.is_(False))
    ).all()
    sub_by_channel = {sub.channel_id: sub for sub, _ in subs}
    channels = {ch.channel_id: ch for _, ch in subs}

    categories = db.execute(select(Category).where(Category.user_id == user.id).order_by(Category.name)).scalars().all()
    cats_by_channel: dict[str, list[Category]] = {}
    cat_by_id = {c.id: c for c in categories}
    for channel_id, category_id in db.execute(
        select(ChannelCategory.channel_id, ChannelCategory.category_id).where(ChannelCategory.user_id == user.id)
    ).all():
        if channel_id in channels and category_id in cat_by_id:
            cats_by_channel.setdefault(channel_id, []).append(cat_by_id[category_id])

    videos = _load_videos(db, channels, since=now - WINDOW)
    _annotate(user, db, videos)

    yt_video_ids = [v["video_id"] for v in videos if v["video_id"]]
    shorts: set[str] = set()
    live: set[str] = set()
    if yt_video_ids:
        shorts = set(
            db.execute(
                select(VideoShort.video_id).where(VideoShort.video_id.in_(yt_video_ids), VideoShort.is_short.is_(True))
            )
            .scalars()
            .all()
        )
        live = set(
            db.execute(
                select(VideoLiveStatus.video_id).where(
                    VideoLiveStatus.video_id.in_(yt_video_ids),
                    VideoLiveStatus.status.in_(("upcoming", "live")),
                )
            )
            .scalars()
            .all()
        )

    def shown_in(v: dict, cat: Category | None) -> bool:
        """Whether the Shorts/live preferences, as cascaded through ``cat``,
        let ``v`` through."""
        sub = sub_by_channel[v["channel_id"]]
        cat_shorts = cat.include_shorts if cat else None
        cat_live = cat.include_live if cat else None
        if v["key"] in shorts and not resolve_include_shorts(sub.include_shorts, cat_shorts, user.include_shorts):
            return False
        return v["key"] not in live or resolve_include_live(sub.include_live, cat_live, user.include_live)

    out_videos = []
    channels_in_cat: dict[str, set[str]] = {}
    for v in videos:
        cats = cats_by_channel.get(v["channel_id"])
        placements: list[tuple[str, Category | None]] = (
            [(f"c{c.id}", c) for c in cats] if cats else [(UNCATEGORIZED, None)]
        )
        cat_ids = [cid for cid, cat in placements if shown_in(v, cat)]
        if not cat_ids:
            continue
        for cid in cat_ids:
            channels_in_cat.setdefault(cid, set()).add(v["channel_id"])
        out_videos.append({**_client_video(v, channels[v["channel_id"]]), "cats": cat_ids})

    def channel_list(cid: str) -> list[dict]:
        chans = [channels[ch] for ch in channels_in_cat.get(cid, ())]
        return [
            {"id": ch.channel_id, "title": ch.title or ch.channel_id, "nebula": ch.platform == nebula.PLATFORM}
            for ch in sorted(chans, key=lambda ch: (ch.title or ch.channel_id).lower())
        ]

    out_cats = [{"id": f"c{c.id}", "name": c.name, "channels": channel_list(f"c{c.id}")} for c in categories]
    if UNCATEGORIZED in channels_in_cat:
        out_cats.append({"id": UNCATEGORIZED, "name": "Uncategorized", "channels": channel_list(UNCATEGORIZED)})

    return {
        "categories": out_cats,
        "videos": out_videos,
        "windowDays": WINDOW.days,
        "now": now.isoformat(),
    }


# A channel page shows its back catalogue as far as it's been stored, but not
# unboundedly: years of a daily uploader would make for a very long pane.
CHANNEL_VIDEO_LIMIT = 60


def channel_videos(user: User, db: Session, channel: Channel) -> list[dict]:
    """One channel's stored videos, newest first, for its channel page.

    Unlike the Watch feed there's no age window and no Shorts/live filtering —
    it's the channel's own page, so it shows what the channel posted.
    """
    videos = _load_videos(db, {channel.channel_id: channel}, since=None, limit=CHANNEL_VIDEO_LIMIT)
    videos = videos[:CHANNEL_VIDEO_LIMIT]
    _annotate(user, db, videos)
    return [_client_video(v, channel) for v in videos]


def set_watched(user: User, db: Session, keys: list[str], watched: bool) -> None:
    """Mark ``keys`` watched or unwatched for ``user``. Idempotent either way."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return
    existing = set(
        db.execute(
            select(WatchedVideo.video_key).where(WatchedVideo.user_id == user.id, WatchedVideo.video_key.in_(keys))
        )
        .scalars()
        .all()
    )
    if watched:
        db.add_all(WatchedVideo(user_id=user.id, video_key=k) for k in keys if k not in existing)
    elif existing:
        db.execute(
            delete(WatchedVideo).where(WatchedVideo.user_id == user.id, WatchedVideo.video_key.in_(list(existing)))
        )
    db.commit()
