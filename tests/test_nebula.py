"""Nebula channels: input parsing, lookup, and the feed-only behaviour — polled
and proxied like YouTube channels, but never filtered or archived. In-memory
SQLite and mocked HTTP; no network."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml import nebula
from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
    ChannelFeedCache,
    Download,
    OpmlToken,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.routes import channels, feed, opml
from youtube_subs_opml.web.services import archive, poller, scheduler
from youtube_subs_opml.youtube import ChannelLookupError

CID = "nebula:tomscott"
TOKEN = "tok"

# Trimmed from the real https://rss.nebula.app/video/channels/tomscott.rss.
FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
<title>Tom Scott: England</title><link>https://nebula.tv/tomscott/</link>
<description>A road trip.</description>
<item><title>Older</title><link>https://nebula.tv/videos/tomscott-older/</link>
<pubDate>Mon, 14 Sep 2026 15:00:00 +0000</pubDate>
<guid isPermaLink="false">video_episode:aaaa</guid></item>
<item><title>Newest</title><link>https://nebula.tv/videos/tomscott-newest/</link>
<pubDate>Mon, 21 Sep 2026 15:00:18 +0000</pubDate>
<guid isPermaLink="false">video_episode:bbbb</guid></item>
</channel></rss>"""

CONTENT = {
    "type": "video_channel",
    "slug": "tomscott",
    "title": "Tom Scott: England",
    "description": "A road trip.",
    "genre_category_title": "Travel",
    "categories": [{"title": "Travel"}, {"title": "Culture"}],
    "images": {
        "avatar": {"src": "https://images.nebula.tv/avatar"},
        "banner": {"src": "https://images.nebula.tv/banner"},
    },
}


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://nebula.tv/tomscott", ("channel", "tomscott")),
        ("https://nebula.tv/tomscott/", ("channel", "tomscott")),
        ("nebula.tv/tomscott", ("channel", "tomscott")),
        ("https://www.nebula.tv/tomscott?ref=x", ("channel", "tomscott")),
        ("nebula:tomscott", ("channel", "tomscott")),
        ("https://rss.nebula.app/video/channels/tomscott.rss", ("channel", "tomscott")),
        ("https://nebula.tv/videos/tomscott-some-video/", ("video", "tomscott-some-video")),
    ],
)
def test_parse_input_recognises_nebula(value, expected):
    assert nebula.parse_input(value) == expected


@pytest.mark.parametrize(
    "value",
    ["tomscott", "@tomscott", "https://www.youtube.com/@tomscott", "UCBa659QWEk1AI4Tg--mrJ2A"],
)
def test_parse_input_leaves_youtube_alone(value):
    assert nebula.parse_input(value) is None


def test_parse_input_rejects_non_channel_rss():
    with pytest.raises(ChannelLookupError):
        nebula.parse_input("https://rss.nebula.app/video.rss")


def test_latest_published_picks_newest_item():
    assert nebula.latest_published(FEED) == datetime(2026, 9, 21, 15, 0, 18, tzinfo=UTC)
    assert nebula.latest_published(b"not xml") is None


# --- lookup ----------------------------------------------------------------


@pytest.fixture
def upstream(monkeypatch):
    """Route nebula.py's httpx traffic to a dict of url -> (status, body)."""
    routes: dict[str, tuple[int, dict | bytes]] = {}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(str(request.url), (404, b""))
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=body)

    monkeypatch.setattr(
        nebula.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    return routes


def test_resolve_uses_content_api(upstream):
    upstream["https://content.api.nebula.app/content/tomscott/"] = (200, CONTENT)
    r = nebula.resolve_channel("https://nebula.tv/tomscott")
    assert r.channel_id == CID
    assert r.platform == "nebula"
    assert r.title == "Tom Scott: England"
    assert r.topics == ["Travel", "Culture"]
    assert r.thumbnail_url == "https://images.nebula.tv/avatar"


def test_resolve_video_url_finds_its_channel(upstream):
    upstream["https://content.api.nebula.app/content/videos/tomscott-x/"] = (
        200,
        {"type": "video_episode", "channel_slug": "tomscott"},
    )
    upstream["https://content.api.nebula.app/content/tomscott/"] = (200, CONTENT)
    assert nebula.resolve_channel("https://nebula.tv/videos/tomscott-x").channel_id == CID


def test_resolve_unknown_channel(upstream):
    with pytest.raises(ChannelLookupError):
        nebula.resolve_channel("https://nebula.tv/nope")


def test_resolve_falls_back_to_feed_when_api_errors(upstream):
    upstream["https://content.api.nebula.app/content/tomscott/"] = (500, b"")
    upstream["https://rss.nebula.app/video/channels/tomscott.rss"] = (200, FEED)
    r = nebula.resolve_channel("nebula.tv/tomscott")
    assert (r.channel_id, r.title, r.topics) == (CID, "Tom Scott: England", None)


# --- database-backed behaviour -----------------------------------------------


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        # Every archive feature switched on at the account level, so anything
        # that leaks through to a Nebula channel shows up.
        s.add(
            User(
                id=1,
                oidc_sub="s",
                email="e",
                include_shorts=False,
                include_live=False,
                download_enabled=True,
                generate_podcast=True,
                link_target="hold",
            )
        )
        s.add(OpmlToken(user_id=1, token=TOKEN))
        s.add(Channel(channel_id=CID, platform="nebula", title="Tom Scott: England"))
        s.add(Subscription(user_id=1, channel_id=CID))
        s.add(Category(id=1, user_id=1, name="Travel", slug="travel", download_enabled=True))
        s.add(ChannelCategory(user_id=1, channel_id=CID, category_id=1))
        s.commit()
    return factory


@pytest.fixture
def db(session_factory):
    with session_factory() as s:
        yield s


def _app(session_factory, *routers) -> TestClient:
    def odb():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    def ou(db: Session = Depends(get_db)):
        return db.get(User, 1)

    app = FastAPI()
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[get_db] = odb
    app.dependency_overrides[get_current_user] = ou
    return TestClient(app, raise_server_exceptions=False)


def test_poll_caches_nebula_feed_without_recording_videos(db, monkeypatch):
    monkeypatch.setattr(
        poller,
        "get_settings",
        lambda: SimpleNamespace(
            poll_max_retries=0,
            poll_channel_delay_seconds=0,
        ),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=FEED)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert poller.poll_channel(CID, db, client) == 0
    assert calls == ["https://rss.nebula.app/video/channels/tomscott.rss"]
    assert db.get(ChannelFeedCache, CID).xml == FEED
    assert db.execute(select(Video)).first() is None


def test_sweep_polls_nebula_first_without_spacing(db, monkeypatch):
    monkeypatch.setattr(
        poller,
        "get_settings",
        lambda: SimpleNamespace(
            poll_max_retries=0,
            poll_channel_delay_seconds=5,
        ),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(poller.time, "sleep", sleeps.append)
    for cid in ("UCaaaa", "UCbbbb"):
        db.add(Channel(channel_id=cid, title=cid))
        db.add(Subscription(user_id=1, channel_id=cid))
    db.add(Channel(channel_id="nebula:jetlag", platform="nebula", title="Jet Lag"))
    db.add(Subscription(user_id=1, channel_id="nebula:jetlag"))
    db.commit()

    polled: list[str] = []
    monkeypatch.setattr(poller, "poll_channel", lambda cid, db, client: polled.append(cid) or 0)
    monkeypatch.setattr(poller, "enqueue_pending", lambda db: 0)
    poller.poll_all_channels(db)

    assert polled == ["nebula:jetlag", "nebula:tomscott", "UCaaaa", "UCbbbb"]
    assert len(sleeps) == 1  # only between the two YouTube channels


def test_nebula_is_never_archived(db):
    # Even a stray Video row (e.g. from a future change) mustn't be enqueued.
    db.add(Video(video_id="neb1", channel_id=CID, title="x"))
    db.commit()
    assert CID not in archive.channel_intents(db)
    assert archive.retained_video_ids(db) == set()
    assert archive.retained_podcast_ids(db) == set()
    assert archive.enqueue_pending(db) == 0
    assert db.execute(select(Download)).first() is None


def test_feed_proxy_passes_nebula_through_unfiltered(session_factory):
    with session_factory() as s:
        s.add(ChannelFeedCache(channel_id=CID, xml=FEED))
        s.commit()
    tc = _app(session_factory, feed.router)
    # Shorts and live are excluded and link_target is "hold" — none of which
    # may touch a Nebula feed.
    for path in (f"/feed/{TOKEN}/{CID}.xml", f"/feed/{TOKEN}/travel/{CID}.xml"):
        resp = tc.get(path)
        assert resp.status_code == 200
        assert resp.content == FEED
        assert resp.headers["content-type"].startswith("application/rss+xml")


def test_feed_proxy_nebula_cache_miss_is_retryable(session_factory):
    resp = _app(session_factory, feed.router).get(f"/feed/{TOKEN}/{CID}.xml")
    assert resp.status_code == 503


def test_opml_links_nebula_channel_page(session_factory, monkeypatch):
    monkeypatch.setattr(opml, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    body = _app(session_factory, opml.router).get(f"/opml/{TOKEN}/all.opml").text
    assert f'xmlUrl="http://test/feed/{TOKEN}/{CID}.xml"' in body
    assert 'htmlUrl="https://nebula.tv/tomscott"' in body


def test_add_nebula_channel(session_factory, monkeypatch, upstream):
    monkeypatch.setattr(channels, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    monkeypatch.setattr(scheduler, "warm_new_channels_soon", lambda: None)

    def no_youtube(value):
        raise AssertionError("a Nebula URL must not reach the YouTube resolver")

    monkeypatch.setattr(channels, "resolve_channel_public", no_youtube)
    upstream["https://content.api.nebula.app/content/realengineering/"] = (
        200,
        {**CONTENT, "slug": "realengineering", "title": "Real Engineering"},
    )
    upstream["https://rss.nebula.app/video/channels/realengineering.rss"] = (200, FEED)
    monkeypatch.setattr(
        poller,
        "get_settings",
        lambda: SimpleNamespace(
            poll_max_retries=0,
            poll_channel_delay_seconds=0,
        ),
    )

    tc = _app(session_factory, channels.router)
    resp = tc.post("/channels/add", data={"channel_input": "https://nebula.tv/realengineering"})
    assert resp.status_code == 200
    assert "NEB" in resp.text

    with session_factory() as s:
        ch = s.get(Channel, "nebula:realengineering")
        assert (ch.platform, ch.title) == ("nebula", "Real Engineering")
        assert ch.youtube_topics == ["Travel", "Culture"]
        sub = s.get(Subscription, (1, "nebula:realengineering"))
        assert sub is not None and sub.account_id is None
        # Warmed on add, so the feed URL works without waiting for a sweep.
        assert s.get(ChannelFeedCache, "nebula:realengineering").xml == FEED


def test_add_unknown_nebula_channel_is_a_400(session_factory, monkeypatch, upstream):
    monkeypatch.setattr(scheduler, "warm_new_channels_soon", lambda: None)
    tc = _app(session_factory, channels.router)
    resp = tc.post("/channels/add", data={"channel_input": "https://nebula.tv/nope"})
    assert resp.status_code == 400
    assert "No Nebula channel" in resp.json()["detail"]


def test_detail_disables_unsupported_features(session_factory, monkeypatch):
    monkeypatch.setattr(channels, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    with session_factory() as s:
        s.add(ChannelFeedCache(channel_id=CID, xml=FEED))
        s.commit()
    html = _app(session_factory, channels.router).get(f"/channels/{CID}/detail").text
    assert "Open on Nebula" in html
    assert "https://nebula.tv/tomscott" in html
    assert "Nebula categories" in html
    # No archive/filter controls are rendered, only the explanatory notes.
    assert "/channels/archive-pref" not in html
    assert "/channels/include-shorts" not in html
    assert "Archive activity" not in html
    assert "Unavailable for Nebula channels" in html


def test_list_marks_nebula_and_never_shows_archiving(session_factory):
    html = _app(session_factory, channels.router).get("/channels/list").text
    assert "NEB" in html
    # Download is on at both the account and category level, but not for Nebula.
    assert "badge-arc" not in html
