from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


@dataclass(frozen=True)
class Subscription:
    channel_id: str
    title: str
    description: str
    include_shorts: bool = True


@dataclass(frozen=True)
class ResolvedChannel:
    channel_id: str
    title: str
    description: str
    topics: list[str] | None


class ChannelLookupError(ValueError):
    """Raised when a user-supplied channel reference cannot be resolved."""


_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._-]+$")


def _parse_channel_input(value: str) -> tuple[str, str]:
    """Classify input as ('id', UC...) or ('handle', '@name').

    Accepts: bare channel id, bare handle, /@handle, /channel/UC..., full URLs.
    """
    value = value.strip()
    if not value:
        raise ChannelLookupError("Empty input")

    if _CHANNEL_ID_RE.match(value):
        return "id", value

    if value.startswith(("http://", "https://", "//")) or value.startswith("youtube.com") or value.startswith("www.youtube.com"):
        url = value if "://" in value else "https://" + value.lstrip("/")
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if path.startswith("channel/"):
            cid = path[len("channel/"):].split("/", 1)[0]
            if _CHANNEL_ID_RE.match(cid):
                return "id", cid
            raise ChannelLookupError(f"Invalid channel id in URL: {cid}")
        if path.startswith("@"):
            handle = path.split("/", 1)[0]
            return "handle", handle
        raise ChannelLookupError(
            "Unsupported URL format. Use a /channel/UC... or /@handle URL."
        )

    if value.startswith("@") or _HANDLE_RE.match(value):
        handle = value if value.startswith("@") else "@" + value
        return "handle", handle

    raise ChannelLookupError(f"Could not interpret '{value}' as a channel reference")


def resolve_channel(creds: Credentials, value: str) -> ResolvedChannel:
    """Resolve a user-supplied channel reference (id/handle/URL) to channel data."""
    kind, key = _parse_channel_input(value)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    params = {"part": "snippet,topicDetails"}
    if kind == "id":
        params["id"] = key
    else:
        params["forHandle"] = key

    response = youtube.channels().list(**params).execute()
    items = response.get("items", [])
    if not items:
        raise ChannelLookupError(f"No channel found for '{value}'")

    item = items[0]
    snippet = item.get("snippet", {})
    topic_urls = item.get("topicDetails", {}).get("topicCategories", []) or []
    topics = [url.rsplit("/", 1)[-1].replace("_", " ") for url in topic_urls] or None

    return ResolvedChannel(
        channel_id=item["id"],
        title=snippet.get("title", ""),
        description=snippet.get("description", ""),
        topics=topics,
    )


def fetch_channel_topics(
    creds: Credentials, channel_ids: list[str]
) -> dict[str, list[str]]:
    """Fetch topic categories for channels. Returns {channel_id: [topic_name, ...]}."""
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    result: dict[str, list[str]] = {}

    # API allows up to 50 IDs per request
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        response = (
            youtube.channels()
            .list(part="topicDetails", id=",".join(batch))
            .execute()
        )
        for item in response.get("items", []):
            topic_details = item.get("topicDetails", {})
            categories = topic_details.get("topicCategories", [])
            # URLs like "https://en.wikipedia.org/wiki/Music" -> "Music"
            topics = [url.rsplit("/", 1)[-1].replace("_", " ") for url in categories]
            if topics:
                result[item["id"]] = topics

    return result


def fetch_subscriptions(creds: Credentials) -> list[Subscription]:
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    subs: list[Subscription] = []
    page_token: str | None = None
    while True:
        response = (
            youtube.subscriptions()
            .list(
                part="snippet",
                mine=True,
                maxResults=50,
                order="alphabetical",
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("items", []):
            snippet = item["snippet"]
            subs.append(
                Subscription(
                    channel_id=snippet["resourceId"]["channelId"],
                    title=snippet["title"],
                    description=snippet.get("description", ""),
                )
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return subs
