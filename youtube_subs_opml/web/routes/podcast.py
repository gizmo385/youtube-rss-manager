"""Podcast (RSS 2.0 + iTunes) feeds and media serving.

A companion to the OPML feeds: where those point a reader at YouTube, these
serve the *audio we already extracted* for archived videos, so a category can be
subscribed to in Apple Podcasts (Library → ⋯ → Add a Show by URL).

Both routes are token-authenticated exactly like the OPML/feed proxy — the token
identifies the user, and we only ever expose channels that user is subscribed
to. The audio itself is served by ``/media/{token}/{video_id}.m4a`` with HTTP
range support (Starlette's ``FileResponse`` handles ``Range`` natively), which
podcast clients require for seeking.

A subscriber who has SponsorBlock remove segments from their podcasts is
served the downloader's cut copy of an episode once there is one, at the same
URL, with the feed giving its size and duration (see services/sponsorblock).
"""

from __future__ import annotations

import os
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from ..config import get_settings
from ..db import get_db
from ..models import (
    AudioCut,
    Category,
    Channel,
    ChannelCategory,
    Download,
    OpmlToken,
    Subscription,
    User,
    Video,
)
from ..services import sponsorblock
from ..services.sources import followed_video_ids, sources_of, video_sources

router = APIRouter(tags=["podcast"])

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", _ITUNES)

# Rich HTML show notes go in <content:encoded>, which Apple Podcasts and others
# prefer over the plain-text <description> (and, unlike it, render links).
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
ET.register_namespace("content", _CONTENT)

# yt-dlp extracts audio as m4a (see downloader.ytdlp.extract_audio).
_AUDIO_MIME = "audio/x-m4a"


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _episode_notes(description: str | None, video_id: str) -> str:
    """Plain-text show notes: the video's description plus a link back to it.

    Always includes the source link even when the description is empty or the
    video hasn't been probed yet, so every episode carries at least that much.
    """
    link = f"Watch on YouTube: {_watch_url(video_id)}"
    body = (description or "").strip()
    return f"{body}\n\n{link}" if body else link


def _episode_notes_html(description: str | None, video_id: str) -> str:
    """The same notes as HTML for <content:encoded>.

    Returned unescaped — ElementTree XML-escapes it exactly once on write, which
    is the correct on-the-wire form for content:encoded.
    """
    body = (description or "").strip()
    parts = []
    if body:
        parts.append("<p>" + body.replace("\n", "<br/>") + "</p>")
    parts.append(f'<p><a href="{_watch_url(video_id)}">Watch on YouTube</a></p>')
    return "".join(parts)


def episode_image_url(video_id: str) -> str:
    """YouTube's stable public thumbnail URL for a video.

    ``hqdefault`` (480x360) always exists for any public video, unlike
    ``maxresdefault``, and needs no auth or storage on our side — so podcast
    clients can render per-episode art without us serving anything.
    """
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _validate_token(token: str, db: Session) -> OpmlToken:
    opml_token = db.execute(select(OpmlToken).where(OpmlToken.token == token)).scalar_one_or_none()
    if opml_token is None:
        raise HTTPException(status_code=404)
    return opml_token


def _fmt_duration(seconds: int | None) -> str | None:
    if not seconds or seconds < 0:
        return None
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = text
    return el


def _itunes(name: str) -> str:
    return f"{{{_ITUNES}}}{name}"


def _content_tag(name: str) -> str:
    return f"{{{_CONTENT}}}{name}"


def build_podcast_feed(
    *,
    title: str,
    description: str,
    site_link: str,
    author: str,
    language: str,
    cover_url: str,
    items: list[dict],
) -> bytes:
    """Serialize an RSS 2.0 + iTunes feed. ``items`` come from ``_podcast_items``."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = _sub(rss, "channel")
    _sub(channel, "title", title)
    _sub(channel, "link", site_link)
    _sub(channel, "description", description or title)
    _sub(channel, "language", language)
    _sub(channel, _itunes("author"), author)
    _sub(channel, _itunes("explicit"), "false")
    if cover_url:
        _sub(channel, _itunes("image"), href=cover_url)

    for item in items:
        it = _sub(channel, "item")
        _sub(it, "title", item["title"])
        _sub(it, "guid", item["video_id"], isPermaLink="false")
        if item["author"]:
            _sub(it, _itunes("author"), item["author"])
        if item["pub_date"] is not None:
            _sub(it, "pubDate", format_datetime(item["pub_date"]))
        _sub(
            it,
            "enclosure",
            url=item["enclosure_url"],
            length=str(item["length"]),
            type=_AUDIO_MIME,
        )
        duration = _fmt_duration(item["duration_seconds"])
        if duration:
            _sub(it, _itunes("duration"), duration)
        # Show notes: the video's description plus a link back to the source.
        # Plain text in <description>/<itunes:summary>; the same as HTML (with a
        # clickable link) in <content:encoded> for clients that render it.
        notes = _episode_notes(item.get("description"), item["video_id"])
        _sub(it, "description", notes)
        _sub(it, _itunes("summary"), notes)
        _sub(it, _content_tag("encoded"), _episode_notes_html(item.get("description"), item["video_id"]))
        # Per-episode art so each entry is visually attributable to its video.
        # Apple ignores item-level <itunes:image>, but Overcast/Pocket Casts
        # honour it, which is exactly where the "who is this from?" gap showed.
        if item.get("image_url"):
            _sub(it, _itunes("image"), href=item["image_url"])

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def _cuts(db: Session, user: User, video_ids: list[str]) -> dict[str, AudioCut]:
    """The SponsorBlock-cut copy to serve ``user`` for each of ``video_ids``
    that has one, by video id. Whether an episode is cut depends on every
    channel and playlist of the user's it's in (``podcast_cut_for``), the
    same as when the downloader decides which cuts to make."""
    kinds_by_sources: dict[frozenset[str], str | None] = {}
    wanted: dict[str, str] = {}
    for vid, sources in sources_of(db, video_ids).items():
        key = frozenset(sources)
        if key not in kinds_by_sources:
            kinds_by_sources[key] = sponsorblock.podcast_cut_for(db, user, key)
        if kinds := kinds_by_sources[key]:
            wanted[vid] = kinds
    if not wanted:
        return {}
    rows = db.execute(select(AudioCut).where(AudioCut.video_id.in_(wanted), AudioCut.path.is_not(None))).scalars()
    return {c.video_id: c for c in rows if wanted[c.video_id] == c.categories}


def _podcast_items(db: Session, user: User, base_url: str, token: str, category_id: int | None) -> list[dict]:
    """Completed downloads with extracted audio, in the user's scope: in a
    channel or playlist they follow (in the category, when there is one).

    Only videos with ``audio_path`` set and a ``complete`` download are eligible;
    the ``length`` is the real on-disk byte count (wrong counts break scrubbing)
    — of the SponsorBlock-cut copy, where that's what will be served. A video
    in more than one of the user's channels and playlists is listed once.
    """
    user_id = user.id
    vs = video_sources()
    uploader = aliased(Channel)
    stmt = (
        select(Video, Download, uploader.title)
        .join(Download, Download.video_id == Video.video_id)
        .join(uploader, uploader.channel_id == Video.channel_id)
        .join(vs, vs.c.video_id == Video.video_id)
        .join(
            Subscription,
            and_(
                Subscription.channel_id == vs.c.source_id,
                Subscription.user_id == user_id,
            ),
        )
        .where(
            Subscription.ignored == False,  # noqa: E712
            Download.status == "complete",
            Download.audio_path.is_not(None),
        )
        .order_by(Video.published_at.desc().nullslast(), Video.video_id)
    )
    if category_id is not None:
        stmt = stmt.join(
            ChannelCategory,
            and_(
                ChannelCategory.channel_id == vs.c.source_id,
                ChannelCategory.user_id == user_id,
                ChannelCategory.category_id == category_id,
            ),
        )

    # One row per (video, source); keep each video's first, in feed order.
    rows = {video.video_id: (video, download, author) for video, download, author in db.execute(stmt).tuples()}
    cuts = _cuts(db, user, list(rows))
    items = []
    for video, download, author in rows.values():
        cut = cuts.get(video.video_id)
        items.append(
            {
                "video_id": video.video_id,
                "title": video.title or video.video_id,
                # Whoever made it, even when it's here through a playlist.
                "author": author,
                "description": video.description,
                "pub_date": video.published_at,
                "duration_seconds": (cut.duration_seconds if cut else None) or video.duration_seconds,
                "length": (cut.size_bytes if cut else download.audio_size_bytes) or 0,
                "enclosure_url": f"{base_url.rstrip('/')}/media/{token}/{video.video_id}.m4a",
                "image_url": episode_image_url(video.video_id),
            }
        )
    return items


def _cover_url(settings, items: list[dict]) -> str:
    """Channel artwork for the feed.

    A configured ``podcast_cover_url`` wins (e.g. a submission-ready square
    image). Otherwise fall back to the newest episode's thumbnail so the show
    still has *some* art rather than a blank tile — items are ordered
    newest-first, so ``items[0]`` is the most recent.
    """
    if settings.podcast_cover_url:
        return settings.podcast_cover_url
    return items[0]["image_url"] if items else ""


@router.get("/podcast/{token}/all.xml")
def podcast_all(token: str, db: Session = Depends(get_db)) -> Response:
    """Podcast feed spanning every subscribed channel's archived audio."""
    opml_token = _validate_token(token, db)
    settings = get_settings()
    user = db.get_one(User, opml_token.user_id)

    items = _podcast_items(db, user, settings.base_url, token, None)
    xml = build_podcast_feed(
        title="Archived videos",
        description="Audio from your archived YouTube subscriptions.",
        site_link=settings.base_url,
        author=user.display_name or "YouTube Archive",
        language=settings.podcast_language,
        cover_url=_cover_url(settings, items),
        items=items,
    )
    return Response(content=xml, media_type="application/rss+xml")


@router.get("/podcast/{token}/{slug}.xml")
def podcast_by_category(token: str, slug: str, db: Session = Depends(get_db)) -> Response:
    """Podcast feed for a single category's archived audio."""
    opml_token = _validate_token(token, db)
    settings = get_settings()
    user = db.get_one(User, opml_token.user_id)

    category = db.execute(
        select(Category).where(
            Category.user_id == user.id,
            Category.slug == slug,
        )
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=404)

    items = _podcast_items(db, user, settings.base_url, token, category.id)
    xml = build_podcast_feed(
        title=category.name,
        description=f"Audio from your archived '{category.name}' subscriptions.",
        site_link=settings.base_url,
        author=user.display_name or "YouTube Archive",
        language=settings.podcast_language,
        cover_url=_cover_url(settings, items),
        items=items,
    )
    return Response(content=xml, media_type="application/rss+xml")


@router.get("/media/{token}/{video_id}.m4a")
def media_audio(token: str, video_id: str, db: Session = Depends(get_db)) -> FileResponse:
    """Serve a video's extracted audio, with HTTP range support for seeking.

    Only serves audio for a video in a channel or playlist the token's user is
    subscribed to, so the token is not an open file proxy.
    """
    opml_token = _validate_token(token, db)

    download = db.execute(
        select(Download).where(
            Download.video_id == video_id,
            Download.video_id.in_(followed_video_ids(opml_token.user_id, include_ignored=True)),
            Download.status == "complete",
            Download.audio_path.is_not(None),
        )
    ).scalar_one_or_none()
    if download is None:
        raise HTTPException(status_code=404)

    cut = _cuts(db, db.get_one(User, opml_token.user_id), [video_id]).get(video_id)
    path = cut.path if cut and cut.path and os.path.isfile(cut.path) else download.audio_path
    if path is None or not os.path.isfile(path):  # the query filters out None; narrows the type
        # The row says complete but the file isn't reachable from this process
        # (e.g. the media volume isn't mounted into the web container).
        raise HTTPException(status_code=404)

    return FileResponse(
        path,
        media_type=_AUDIO_MIME,
        filename=f"{video_id}.m4a",
    )
