"""YouTube Data API helpers, driven through googleapiclient's HTTP mocks.

The client is built from the discovery document bundled with
google-api-python-client, so no network access is needed.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from youtube_subs_opml import youtube as yt


def _subscription(channel_id: str, title: str) -> dict:
    return {"snippet": {"resourceId": {"channelId": channel_id}, "title": title}}


def _mock_youtube(monkeypatch, pages: list[dict]) -> HttpMockSequence:
    http = HttpMockSequence([({"status": "200"}, json.dumps(page)) for page in pages])
    # The stubs' build() overloads accept HttpMock but omit HttpMockSequence.
    client = build("youtube", "v3", http=http)  # pyrefly: ignore[no-matching-overload]
    monkeypatch.setattr(yt, "build", lambda *args, **kwargs: client)
    return http


def test_fetch_subscriptions_follows_page_tokens(monkeypatch):
    http = _mock_youtube(
        monkeypatch,
        [
            {"items": [_subscription("UC1", "One")], "nextPageToken": "page-2"},
            {"items": [_subscription("UC2", "Two")]},
        ],
    )

    subs = yt.fetch_subscriptions(Credentials(token="unused"))

    assert [(s.channel_id, s.title, s.description) for s in subs] == [("UC1", "One", ""), ("UC2", "Two", "")]
    first, second = (parse_qs(urlparse(uri).query) for uri, *_ in http.request_sequence)
    assert "pageToken" not in first
    assert second["pageToken"] == ["page-2"]


def test_fetch_subscriptions_single_page(monkeypatch):
    http = _mock_youtube(monkeypatch, [{"items": [_subscription("UC1", "One")]}])

    subs = yt.fetch_subscriptions(Credentials(token="unused"))

    assert [s.channel_id for s in subs] == ["UC1"]
    assert len(http.request_sequence) == 1
