"""A channel's recent uploads from the YouTube Data API, shaped like its RSS feed.

Fallback for when YouTube's RSS endpoint (``/feeds/videos.xml``) refuses this
server, which it sometimes does for every channel for hours at a time while the
rest of YouTube (and the Data API) keeps working.

A channel's uploads are the public playlist ``UU<id>``, so an API key is enough;
no linked account, and so nothing that expires. Each call costs 1 unit of the
default 10,000/day quota, which is why the poller limits how often it falls back
per channel.

The result mimics the RSS feed closely enough that everything downstream (the
poller's entry parsing, the feed cache, the proxy's filtering) treats it the
same. Entry ``<id>``\\ s match the real feed's, so a reader doesn't see duplicates
when a channel switches between the two.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx

from youtube_subs_opml.opml import FEED_URL

_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
# What the RSS feed carries.
_MAX_RESULTS = 15

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_MEDIA = "http://search.yahoo.com/mrss/"

ET.register_namespace("", _ATOM)
ET.register_namespace("yt", _YT)
ET.register_namespace("media", _MEDIA)


def uploads_url(channel_id: str) -> str:
    """The playlistItems request for a channel's most recent uploads."""
    params = {
        "part": "snippet,contentDetails",
        "playlistId": "UU" + channel_id.removeprefix("UC"),
        "maxResults": _MAX_RESULTS,
    }
    return f"{_PLAYLIST_ITEMS_URL}?{urlencode(params)}"


def fetch_uploads_feed(channel_id: str, client: httpx.Client, api_key: str, *, title: str = "") -> bytes:
    """Fetch a channel's recent uploads and render them as its RSS feed would be.

    The key goes in a header rather than the query string so it stays out of
    traced URLs. Raises ``httpx.HTTPError`` on failure (quota exhausted, bad key,
    unknown channel) for the caller to log.
    """
    resp = client.get(uploads_url(channel_id), headers={"X-Goog-Api-Key": api_key})
    resp.raise_for_status()
    return build_feed(channel_id, resp.json().get("items", []), title=title)


def error_reason(resp: httpx.Response) -> str:
    """Google's machine-readable reason for a failed call (e.g.
    ``API_KEY_HTTP_REFERRER_BLOCKED``, ``quotaExceeded``), which the status
    code alone doesn't tell apart. Empty if the body isn't Google's error JSON."""
    try:
        error = resp.json()["error"]
    except (ValueError, KeyError, TypeError):
        return ""
    for detail in error.get("details", []):
        if detail.get("reason"):
            return detail["reason"]
    for item in error.get("errors", []):
        if item.get("reason"):
            return item["reason"]
    return error.get("status", "")


def _iso(value: str) -> str:
    """The API's ``...Z`` timestamps in the feed's ``+00:00`` form."""
    return datetime.fromisoformat(value).isoformat()


def _sub(parent: ET.Element, ns: str, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ns}}}{tag}", attrs)
    if text is not None:
        el.text = text
    return el


def build_feed(channel_id: str, items: list[dict], *, title: str = "") -> bytes:
    """Render playlistItems results as an Atom feed in YouTube's RSS layout."""
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    title = next(
        (i["snippet"]["channelTitle"] for i in items if i.get("snippet", {}).get("channelTitle")),
        title,
    )

    feed = ET.Element(f"{{{_ATOM}}}feed")
    _sub(feed, _ATOM, "link", rel="self", href=FEED_URL.format(channel_id=channel_id))
    # The real feed drops the UC prefix here (but not in entries).
    _sub(feed, _ATOM, "id", f"yt:channel:{channel_id.removeprefix('UC')}")
    _sub(feed, _YT, "channelId", channel_id.removeprefix("UC"))
    _sub(feed, _ATOM, "title", title)
    _sub(feed, _ATOM, "link", rel="alternate", href=channel_url)
    author = _sub(feed, _ATOM, "author")
    _sub(author, _ATOM, "name", title)
    _sub(author, _ATOM, "uri", channel_url)

    for item in items:
        snippet = item.get("snippet", {})
        video_id = snippet.get("resourceId", {}).get("videoId")
        # Private and deleted videos can linger in the playlist without an owner.
        if not video_id or "videoOwnerChannelId" not in snippet:
            continue
        published = _iso(item.get("contentDetails", {}).get("videoPublishedAt") or snippet["publishedAt"])
        video_title = snippet.get("title", "")

        entry = _sub(feed, _ATOM, "entry")
        _sub(entry, _ATOM, "id", f"yt:video:{video_id}")
        _sub(entry, _YT, "videoId", video_id)
        _sub(entry, _YT, "channelId", channel_id)
        _sub(entry, _ATOM, "title", video_title)
        _sub(entry, _ATOM, "link", rel="alternate", href=f"https://www.youtube.com/watch?v={video_id}")
        author = _sub(entry, _ATOM, "author")
        _sub(author, _ATOM, "name", title)
        _sub(author, _ATOM, "uri", channel_url)
        _sub(entry, _ATOM, "published", published)
        _sub(entry, _ATOM, "updated", published)
        group = _sub(entry, _MEDIA, "group")
        _sub(group, _MEDIA, "title", video_title)
        _sub(
            group,
            _MEDIA,
            "content",
            url=f"https://www.youtube.com/v/{video_id}?version=3",
            type="application/x-shockwave-flash",
            width="640",
            height="390",
        )
        _sub(
            group,
            _MEDIA,
            "thumbnail",
            url=f"https://i1.ytimg.com/vi/{video_id}/hqdefault.jpg",
            width="480",
            height="360",
        )
        _sub(group, _MEDIA, "description", snippet.get("description", ""))

    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)
