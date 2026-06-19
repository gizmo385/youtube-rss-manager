from __future__ import annotations

import re
from xml.etree import ElementTree as ET

import httpx

from youtube_subs_opml.youtube import (
    ChannelLookupError,
    ResolvedChannel,
    _parse_channel_input,
)

_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
_ATOM = "http://www.w3.org/2005/Atom"
_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Matches the channel id from a channel page: canonical /channel/UC… link or
# the channelId/externalId keys in the embedded JSON.
_CHANNEL_ID_RE = re.compile(
    r'(?:"channelId":"|"externalId":"|/channel/)(UC[A-Za-z0-9_-]{22})'
)


def _fetch_title(channel_id: str, client: httpx.Client) -> str:
    """Read a channel's display name from its public RSS feed."""
    resp = client.get(_FEED_URL.format(cid=channel_id), follow_redirects=True)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    return root.findtext(f"{{{_ATOM}}}title") or channel_id


def resolve_channel_public(value: str) -> ResolvedChannel:
    """Resolve a channel reference (id/handle/URL) using only public HTTP.

    No API key or OAuth needed — useful for manual channel adds when no YouTube
    account is connected. Topics/description aren't available this way.

    Raises ChannelLookupError on bad input or if the channel can't be found.
    """
    kind, key = _parse_channel_input(value)
    with httpx.Client(
        headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT
    ) as client:
        if kind == "id":
            channel_id = key
        else:
            # Handle/URL: load the channel page and extract its channel id.
            page = client.get(
                f"https://www.youtube.com/{key.lstrip('/')}",
                follow_redirects=True,
            )
            if page.status_code == 404:
                raise ChannelLookupError(f"No channel found for '{value}'")
            page.raise_for_status()
            match = _CHANNEL_ID_RE.search(page.text)
            if match is None:
                raise ChannelLookupError(
                    f"Could not resolve a channel id for '{value}'"
                )
            channel_id = match.group(1)

        try:
            title = _fetch_title(channel_id, client)
        except httpx.HTTPError:
            title = channel_id

    return ResolvedChannel(
        channel_id=channel_id,
        title=title,
        description="",
        topics=None,
    )
