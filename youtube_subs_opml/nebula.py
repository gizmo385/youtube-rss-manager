"""Nebula channel support: input parsing, lookup, and feed URLs.

Nebula publishes an official RSS 2.0 feed per channel
(https://blog.nebula.tv/rss-feed/), keyed by the channel's URL slug. That's
enough to follow a channel through the feed proxy, but it's all we get: there's
no account sync, the feed carries no duration or Shorts/livestream markers, and
videos can't be downloaded — so Nebula channels support categories, OPML and
ignoring, and nothing from the archive side.

Nebula channels are stored in the same ``channels`` table as YouTube ones, with
``platform='nebula'`` and a ``channel_id`` of ``nebula:{slug}``. The prefix keeps
the id space disjoint from YouTube's ``UC…`` ids and makes the id
self-describing wherever it appears (URLs, logs, the feed proxy).
"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from .youtube import ChannelLookupError, ResolvedChannel

PLATFORM = "nebula"
ID_PREFIX = "nebula:"

FEED_URL = "https://rss.nebula.app/video/channels/{slug}.rss"
CHANNEL_URL = "https://nebula.tv/{slug}"
_CONTENT_API = "https://content.api.nebula.app/content/{path}/"

_HOSTS = frozenset({"nebula.tv", "www.nebula.tv", "nebula.app", "www.nebula.app"})
_RSS_HOST = "rss.nebula.app"
_TIMEOUT = 15.0
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) youtube-rss-manager"


def channel_id_for(slug: str) -> str:
    return f"{ID_PREFIX}{slug}"


def slug_of(channel_id: str) -> str:
    return channel_id.removeprefix(ID_PREFIX)


def feed_url(channel_id: str) -> str:
    return FEED_URL.format(slug=slug_of(channel_id))


def channel_url(channel_id: str) -> str:
    return CHANNEL_URL.format(slug=slug_of(channel_id))


def parse_input(value: str) -> tuple[str, str] | None:
    """Classify input as a Nebula reference, or None if it isn't one.

    Returns ``('channel', slug)`` or ``('video', video_slug)``. Accepts
    ``nebula:{slug}``, channel URLs (``nebula.tv/{slug}``), video URLs
    (``nebula.tv/videos/{slug}``) and the channel's own RSS URL. Anything else —
    including a bare word, which is ambiguous with a YouTube handle — is left for
    the YouTube resolver.
    """
    value = value.strip()
    if value.lower().startswith(ID_PREFIX):
        slug = value[len(ID_PREFIX) :].strip("/")
        return ("channel", slug) if slug else None

    url = value if "://" in value else "https://" + value.lstrip("/")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    if host == _RSS_HOST:
        # /video/channels/{slug}.rss
        if len(parts) == 3 and parts[:2] == ["video", "channels"] and parts[2].endswith(".rss"):
            return "channel", parts[2].removesuffix(".rss")
        raise ChannelLookupError("That Nebula RSS URL isn't a channel feed.")

    if host not in _HOSTS:
        return None
    if not parts:
        raise ChannelLookupError("Paste a Nebula channel URL, e.g. https://nebula.tv/tomscott")
    if parts[0] == "videos" and len(parts) > 1:
        return "video", parts[1]
    return "channel", parts[0]


def _topics(data: dict) -> list[str] | None:
    """Nebula's genre plus its category tags, deduplicated, in that order."""
    names = [data.get("genre_category_title")]
    names += [c.get("title") for c in data.get("categories") or []]
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return seen or None


def _image(data: dict, key: str) -> str | None:
    return ((data.get("images") or {}).get(key) or {}).get("src") or None


def _resolve_via_feed(slug: str, client: httpx.Client) -> ResolvedChannel:
    """Fallback when the content API is unavailable: the RSS feed has the title."""
    resp = client.get(FEED_URL.format(slug=slug))
    if resp.status_code == 404:
        raise ChannelLookupError(f"No Nebula channel found for '{slug}'")
    resp.raise_for_status()
    channel = ET.fromstring(resp.content).find("channel")
    title = (channel.findtext("title") if channel is not None else None) or slug
    description = (channel.findtext("description") if channel is not None else None) or ""
    return ResolvedChannel(
        channel_id=channel_id_for(slug),
        title=title,
        description=description,
        topics=None,
        platform=PLATFORM,
    )


def resolve_channel(value: str) -> ResolvedChannel:
    """Resolve a Nebula channel or video reference to its channel.

    Uses Nebula's public content API (title, description, genre, art). If that
    fails for a reason other than "not found", falls back to the channel's RSS
    feed, which at least carries the title. Raises ChannelLookupError on bad
    input or an unknown channel, ``httpx.HTTPError`` if Nebula is unreachable.
    """
    parsed = parse_input(value)
    if parsed is None:
        raise ChannelLookupError(f"'{value}' isn't a Nebula URL")
    kind, slug = parsed

    with httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True) as client:
        if kind == "video":
            resp = client.get(_CONTENT_API.format(path=f"videos/{slug}"))
            if resp.status_code == 404:
                raise ChannelLookupError(f"No Nebula video found for '{value}'")
            resp.raise_for_status()
            slug = resp.json().get("channel_slug") or ""
            if not slug:
                raise ChannelLookupError(f"Couldn't find the channel for '{value}'")

        try:
            resp = client.get(_CONTENT_API.format(path=slug))
        except httpx.RequestError:
            return _resolve_via_feed(slug, client)
        if resp.status_code == 404:
            raise ChannelLookupError(f"No Nebula channel found for '{value}'")
        if resp.status_code >= 400:
            return _resolve_via_feed(slug, client)

        data = resp.json()
        if data.get("type") != "video_channel":
            raise ChannelLookupError(f"'{value}' isn't a Nebula video channel")
        # The API's slug is canonical; the input may have used an alias.
        slug = data.get("slug") or slug
        return ResolvedChannel(
            channel_id=channel_id_for(slug),
            title=data.get("title") or slug,
            description=data.get("description") or "",
            topics=_topics(data),
            platform=PLATFORM,
            thumbnail_url=_image(data, "avatar"),
            banner_url=_image(data, "banner"),
        )


def latest_published(xml: bytes) -> datetime | None:
    """The newest ``pubDate`` in a Nebula RSS feed, or None if there isn't one.

    Nebula channels aren't recorded in ``videos`` (nothing there is
    downloadable), so this stands in for "last video" on the detail pane.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    newest: datetime | None = None
    for text in root.iterfind("channel/item/pubDate"):
        try:
            dt = parsedate_to_datetime(text.text or "")
        except (TypeError, ValueError):
            continue
        if newest is None or dt > newest:
            newest = dt
    return newest
