"""Poller politeness: browser UA, retry-with-backoff on throttling, and a
spaced-out sweep so ~50 channels don't burst YouTube into rate-limiting."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.opml import FEED_URL
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import Channel, ChannelFeedCache, Subscription, Video
from youtube_subs_opml.web.services import poller, uploads_api

FEED = (
    b'<?xml version="1.0"?>'
    b'<feed xmlns="http://www.w3.org/2005/Atom" '
    b'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
    b"<entry><yt:videoId>vidAAAA1111</yt:videoId><title>Hi</title>"
    b"<published>2026-08-20T09:00:00+00:00</published></entry></feed>"
)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setenv("POLL_CHANNEL_DELAY_SECONDS", "0")  # no real sleeping
    monkeypatch.setenv("POLL_MAX_RETRIES", "2")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    config.get_settings.cache_clear()
    poller._api_fallback_at.clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[Base.metadata.tables[t.__tablename__] for t in (Channel, Video, Subscription, ChannelFeedCache)],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _resp(status: int, url: str, content: bytes = b"") -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("GET", url))


class FakeClient:
    """Serves a scripted list of responses per URL, in order."""

    def __init__(self, script: dict[str, list[httpx.Response]]):
        self.script = script
        self.calls: list[str] = []
        self.sent_headers: list[dict[str, str]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        self.calls.append(url)
        self.sent_headers.append(headers or {})
        return self.script[url].pop(0)

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def test_user_agent_is_browser_like():
    assert "Mozilla" in poller._new_client().headers["User-Agent"]


def test_retries_transient_then_succeeds(db, monkeypatch):
    url = FEED_URL.format(channel_id="UCchannel00000000000001")
    fake = FakeClient({url: [_resp(404, url), _resp(200, url, FEED)]})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    new = poller.poll_channel("UCchannel00000000000001", db)

    assert new == 1  # recovered on retry and parsed the entry
    assert len(fake.calls) == 2
    assert db.execute(select(Video.video_id)).scalars().all() == ["vidAAAA1111"]


def test_poll_warms_the_feed_cache(db, monkeypatch):
    cid = "UCchannel00000000000001"
    url = FEED_URL.format(channel_id=cid)
    fake = FakeClient({url: [_resp(200, url, FEED)]})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    poller.poll_channel(cid, db)
    db.commit()

    # The raw XML is stored so the feed proxy can serve it without hitting YouTube.
    assert db.get(ChannelFeedCache, cid).xml == FEED


def test_failed_poll_does_not_touch_cache(db, monkeypatch):
    cid = "UCchannel00000000000009"
    url = FEED_URL.format(channel_id=cid)
    fake = FakeClient({url: [_resp(500, url), _resp(500, url), _resp(500, url)]})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    assert poller.poll_channel(cid, db) == 0
    db.commit()
    assert db.get(ChannelFeedCache, cid) is None  # nothing cached on failure


def test_gives_up_after_max_retries(db, monkeypatch):
    url = FEED_URL.format(channel_id="UCchannel00000000000002")
    fake = FakeClient({url: [_resp(500, url), _resp(500, url), _resp(500, url)]})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    new = poller.poll_channel("UCchannel00000000000002", db)

    assert new == 0  # logged and skipped, not raised
    assert len(fake.calls) == 3  # initial + 2 retries


def test_poll_all_spaces_out_requests(db, monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("POLL_CHANNEL_DELAY_SECONDS", "3")
    config.get_settings.cache_clear()

    c1 = "UCchannel00000000000001"
    c2 = "UCchannel00000000000002"
    db.add(Subscription(user_id=1, channel_id=c1))
    db.add(Subscription(user_id=1, channel_id=c2))
    db.commit()

    fake = FakeClient(
        {
            FEED_URL.format(channel_id=c1): [_resp(200, "u", FEED)],
            FEED_URL.format(channel_id=c2): [_resp(200, "u", FEED)],
        }
    )
    monkeypatch.setattr(poller, "_new_client", lambda: fake)
    monkeypatch.setattr(poller, "enqueue_pending", lambda db: 0)  # isolate spacing
    sleeps: list[float] = []
    monkeypatch.setattr(poller.time, "sleep", lambda s: sleeps.append(s))

    poller.poll_all_channels(db)

    # Two channels -> exactly one inter-channel sleep, and it respects the delay.
    assert len(sleeps) == 1
    assert sleeps[0] >= 3.0


def test_warm_polls_only_uncached_channels(db, monkeypatch):
    cached, fresh, ignored = "UCcached", "UCfresh", "UCignored"
    db.add(Subscription(user_id=1, channel_id=cached))
    db.add(Subscription(user_id=1, channel_id=fresh))
    db.add(Subscription(user_id=1, channel_id=ignored, ignored=True))
    db.add(ChannelFeedCache(channel_id=cached, xml=FEED))
    db.commit()

    url = FEED_URL.format(channel_id=fresh)
    fake = FakeClient({url: [_resp(200, url, FEED)]})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)
    monkeypatch.setattr(poller, "enqueue_pending", lambda db: 0)

    assert poller.warm_uncached_channels(db) == 1

    assert fake.calls == [url]
    assert db.get(ChannelFeedCache, fresh).xml == FEED


def test_warm_tries_a_failing_channel_once(db, monkeypatch):
    cid = "UCchannel00000000000009"
    db.add(Subscription(user_id=1, channel_id=cid))
    db.commit()
    url = FEED_URL.format(channel_id=cid)
    fake = FakeClient({url: [_resp(500, url)] * 3})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    # Still uncached afterwards, but not re-polled in a loop: the sweep owns it now.
    assert poller.warm_uncached_channels(db) == 0
    assert len(fake.calls) == 3  # initial + 2 retries, once


def test_warm_picks_up_channels_added_mid_run(db, monkeypatch):
    first, second = "UCfirst", "UCsecond"
    db.add(Subscription(user_id=1, channel_id=first))
    db.commit()

    polled: list[str] = []

    def poll(cid, db, client):
        polled.append(cid)
        if cid == first:
            # Another request subscribes to a new channel while this job runs.
            db.add(Subscription(user_id=1, channel_id=second))
        db.add(ChannelFeedCache(channel_id=cid, xml=FEED))
        return 0

    monkeypatch.setattr(poller, "poll_channel", poll)
    poller.warm_uncached_channels(db)

    assert polled == [first, second]


# --- Data API fallback -------------------------------------------------------

API_ITEMS = {
    "items": [
        {
            "snippet": {
                "publishedAt": "2026-09-26T22:30:00Z",
                "channelTitle": "Fallback Channel",
                "title": "From the API",
                "description": "desc",
                "videoOwnerChannelId": "UCchannel00000000000001",
                "resourceId": {"kind": "youtube#video", "videoId": "vidBBBB2222"},
            },
            "contentDetails": {
                "videoId": "vidBBBB2222",
                "videoPublishedAt": "2026-09-26T22:26:23Z",
            },
        },
    ]
}


def _api_client(cid: str, rss: list[httpx.Response], api: list[httpx.Response]) -> FakeClient:
    return FakeClient(
        {
            FEED_URL.format(channel_id=cid): rss,
            uploads_api.uploads_url(cid): api,
        }
    )


def _with_api_key(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    config.get_settings.cache_clear()


def test_failed_rss_falls_back_to_the_data_api(db, monkeypatch):
    _with_api_key(monkeypatch)
    cid = "UCchannel00000000000001"
    api_url = uploads_api.uploads_url(cid)
    fake = _api_client(
        cid, [_resp(404, "u")] * 3, [httpx.Response(200, json=API_ITEMS, request=httpx.Request("GET", api_url))]
    )
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    assert poller.poll_channel(cid, db) == 1
    db.commit()

    assert fake.calls[-1] == api_url
    # The key travels in a header, not the (traced) URL.
    assert fake.sent_headers[-1] == {"X-Goog-Api-Key": "test-key"}
    assert "test-key" not in api_url
    video = db.get(Video, "vidBBBB2222")
    assert video.title == "From the API"
    assert video.published_at.isoformat().startswith("2026-09-26T22:26:23")
    assert b"yt:video:vidBBBB2222" in db.get(ChannelFeedCache, cid).xml


def test_api_fallback_is_rate_limited_per_channel(db, monkeypatch):
    _with_api_key(monkeypatch)
    cid = "UCchannel00000000000001"
    api_url = uploads_api.uploads_url(cid)
    ok = httpx.Response(200, json=API_ITEMS, request=httpx.Request("GET", api_url))
    fake = _api_client(cid, [_resp(404, "u")] * 6, [ok])
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    poller.poll_channel(cid, db)
    poller.poll_channel(cid, db)  # next sweep, well inside the hour

    assert fake.calls.count(api_url) == 1


def test_api_failure_counts_toward_the_interval(db, monkeypatch):
    _with_api_key(monkeypatch)
    cid = "UCchannel00000000000001"
    api_url = uploads_api.uploads_url(cid)
    fake = _api_client(cid, [_resp(404, "u")] * 6, [_resp(403, api_url)])
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    assert poller.poll_channel(cid, db) == 0
    assert poller.poll_channel(cid, db) == 0  # an exhausted quota isn't retried each sweep
    db.commit()

    assert fake.calls.count(api_url) == 1
    assert db.get(ChannelFeedCache, cid) is None


def test_no_api_key_means_no_fallback(db, monkeypatch):
    cid = "UCchannel00000000000001"
    fake = _api_client(cid, [_resp(404, "u")] * 3, [])
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    assert poller.poll_channel(cid, db) == 0
    assert uploads_api.uploads_url(cid) not in fake.calls


def test_nebula_never_falls_back(db, monkeypatch):
    from youtube_subs_opml import nebula

    _with_api_key(monkeypatch)
    cid = "nebula-channel"
    db.add(Channel(channel_id=cid, title="Neb", platform=nebula.PLATFORM))
    db.commit()
    url = nebula.feed_url(cid)
    fake = FakeClient({url: [_resp(500, url)] * 3})
    monkeypatch.setattr(poller, "_new_client", lambda: fake)

    assert poller.poll_channel(cid, db) == 0
    assert fake.calls == [url] * 3
