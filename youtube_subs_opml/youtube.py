from __future__ import annotations

from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


@dataclass(frozen=True)
class Subscription:
    channel_id: str
    title: str
    description: str
    include_shorts: bool = True


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
