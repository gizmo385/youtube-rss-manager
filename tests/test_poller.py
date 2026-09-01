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
from youtube_subs_opml.web.models import ChannelFeedCache, Subscription, Video
from youtube_subs_opml.web.services import poller

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
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[Video.__table__, Subscription.__table__, ChannelFeedCache.__table__],
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
        self.headers: dict[str, str] = {}

    def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
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

    assert new == 0                 # logged and skipped, not raised
    assert len(fake.calls) == 3     # initial + 2 retries


def test_poll_all_spaces_out_requests(db, monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("POLL_CHANNEL_DELAY_SECONDS", "3")
    config.get_settings.cache_clear()

    c1 = "UCchannel00000000000001"
    c2 = "UCchannel00000000000002"
    db.add(Subscription(user_id=1, channel_id=c1))
    db.add(Subscription(user_id=1, channel_id=c2))
    db.commit()

    fake = FakeClient({
        FEED_URL.format(channel_id=c1): [_resp(200, "u", FEED)],
        FEED_URL.format(channel_id=c2): [_resp(200, "u", FEED)],
    })
    monkeypatch.setattr(poller, "_new_client", lambda: fake)
    monkeypatch.setattr(poller, "enqueue_pending", lambda db: 0)  # isolate spacing
    sleeps: list[float] = []
    monkeypatch.setattr(poller.time, "sleep", lambda s: sleeps.append(s))

    poller.poll_all_channels(db)

    # Two channels -> exactly one inter-channel sleep, and it respects the delay.
    assert len(sleeps) == 1
    assert sleeps[0] >= 3.0
