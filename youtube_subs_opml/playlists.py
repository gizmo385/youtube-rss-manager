"""YouTube playlist support: input parsing, lookup, and feed URLs.

Some channels publish several shows and keep each in its own playlist, so
following a playlist instead of the channel gets just the shows you want.
YouTube serves a public Atom feed per playlist in the same layout as a
channel's, so playlists are polled, cached and proxied (Shorts and live filters
included) like channels.

Playlists are stored in the ``channels`` table with ``platform='youtube_playlist'``
and their YouTube id (``PL…``) as ``channel_id``. Channel ids are always
``UC…``, so the two id spaces can't collide. A playlist's videos still belong to
the channel that uploaded them (``Video.channel_id``), through a stub
``channels`` row if nobody follows that channel. Which playlists a video is in
is recorded separately in ``playlist_videos``, so one video can be in a followed
channel and in several followed playlists at once.

For now playlists are feed-only, like Nebula channels: no archive, podcast audio
or history backfill.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import httpx

from .youtube import ChannelLookupError, ResolvedChannel

PLATFORM = "youtube_playlist"

FEED_URL = "https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
PAGE_URL = "https://www.youtube.com/playlist?list={playlist_id}"

# Ordinary user-made playlists. Not mixes (RD…), Watch Later or Liked videos,
# which have no public feed, nor a channel's own upload playlists (UU…), which
# are better followed as the channel.
_PLAYLIST_ID_RE = re.compile(r"^PL[A-Za-z0-9_-]{10,}$")
_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"})
_OG_RE = re.compile(r'<meta property="og:(title|description)" content="([^"]*)"')

_ATOM = "http://www.w3.org/2005/Atom"
_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def feed_url(playlist_id: str) -> str:
    return FEED_URL.format(playlist_id=playlist_id)


def page_url(playlist_id: str) -> str:
    return PAGE_URL.format(playlist_id=playlist_id)


def parse_input(value: str) -> str | None:
    """The playlist id ``value`` refers to, or None if it isn't a playlist.

    Accepts a bare ``PL…`` id and any YouTube URL with a ``list=`` (a playlist
    page, or a video watched from one) or a ``playlist_id=`` (the playlist's
    own feed). Anything else is left for the channel resolver. Raises
    ChannelLookupError for a URL naming a kind of playlist that can't be
    followed.
    """
    value = value.strip()
    if _PLAYLIST_ID_RE.match(value):
        return value
    if not value.startswith(("http://", "https://", "//", *_HOSTS)):
        return None

    url = value if "://" in value else "https://" + value.lstrip("/")
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in _HOSTS:
        return None
    query = parse_qs(parsed.query)
    ids = query.get("list") or query.get("playlist_id")
    if not ids:
        return None
    if not _PLAYLIST_ID_RE.match(ids[0]):
        raise ChannelLookupError(
            "Only regular playlists (with ids starting PL) can be followed. Mixes, "
            "Watch Later and a channel's uploads have no playlist feed to follow."
        )
    return ids[0]


def _from_page(playlist_id: str, client: httpx.Client) -> ResolvedChannel | None:
    """Title and description from the playlist's page, or None if it has none.

    YouTube answers 200 for a playlist that doesn't exist or is private, just
    without a real title (``og:title`` is literally "undefined").
    """
    resp = client.get(page_url(playlist_id))
    resp.raise_for_status()
    og = {key: html.unescape(text) for key, text in _OG_RE.findall(resp.text)}
    title = og.get("title", "").strip()
    if not title or title == "undefined":
        return None
    return ResolvedChannel(
        channel_id=playlist_id,
        title=title,
        description=og.get("description", ""),
        topics=None,
        platform=PLATFORM,
    )


def _from_feed(playlist_id: str, client: httpx.Client) -> ResolvedChannel:
    """Fallback for when the page can't be read: the feed has the title too."""
    resp = client.get(feed_url(playlist_id))
    if resp.status_code == 404:
        raise ChannelLookupError(f"No public playlist found for '{playlist_id}'")
    resp.raise_for_status()
    title = ET.fromstring(resp.content).findtext(f"{{{_ATOM}}}title") or playlist_id
    return ResolvedChannel(channel_id=playlist_id, title=title, description="", topics=None, platform=PLATFORM)


def resolve_playlist(value: str) -> ResolvedChannel:
    """Resolve a playlist reference to its id, title and description.

    Reads the playlist's public page, which stays reachable while YouTube's
    feeds are refusing this server, and falls back to the feed if the page
    fails. Raises
    ChannelLookupError on bad input or a missing/private playlist, and
    ``httpx.HTTPError`` if YouTube can't be reached.
    """
    playlist_id = parse_input(value)
    if playlist_id is None:
        raise ChannelLookupError(f"'{value}' isn't a YouTube playlist")
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    with httpx.Client(headers=headers, timeout=_TIMEOUT, follow_redirects=True) as client:
        try:
            resolved = _from_page(playlist_id, client)
        except httpx.HTTPError:
            return _from_feed(playlist_id, client)
    if resolved is None:
        raise ChannelLookupError(f"No public playlist found for '{value}'")
    return resolved
