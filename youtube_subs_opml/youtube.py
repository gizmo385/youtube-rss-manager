from __future__ import annotations

from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


@dataclass(frozen=True)
class Subscription:
    channel_id: str
    title: str
    description: str


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
