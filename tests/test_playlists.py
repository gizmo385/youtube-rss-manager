"""YouTube playlists: input parsing, lookup, polling into ``playlist_videos``,
and following one like a channel — feeds, OPML and the Watch tab, but no
archive. In-memory SQLite and mocked HTTP; no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml import playlists
from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
    ChannelFeedCache,
    OpmlToken,
    PlaylistVideo,
    Subscription,
    User,
    Video,
    VideoShort,
)
from youtube_subs_opml.web.routes import channels, feed, opml
from youtube_subs_opml.web.services import archive, poller, scheduler, uploads_api
from youtube_subs_opml.web.services.watch import build_watch_feed, channel_videos, search_videos
from youtube_subs_opml.youtube import ChannelLookupError

PID = "PLrAXtmErZgOdP_8GztsuKi9nrraNbKKp4"
OWNER = "UCSHZKyawb77ixDdsGog4iWA"
GUEST = "UCguestguestguestguest01"
TOKEN = "tok"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _feed(*entries: tuple[str, str, str, datetime]) -> bytes:
    """A playlist feed in YouTube's layout: (video id, uploader id, uploader
    name, published) per entry."""
    body = "".join(
        f"<entry><id>yt:video:{vid}</id><yt:videoId>{vid}</yt:videoId><yt:channelId>{cid}</yt:channelId>"
        f"<title>Episode {vid}</title><author><name>{name}</name></author>"
        f"<published>{published.isoformat()}</published></entry>"
        for vid, cid, name, published in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">'
        f"<id>yt:playlist:{PID}</id><yt:playlistId>{PID}</yt:playlistId>"
        f"<title>Lex Fridman Podcast</title>{body}</feed>"
    ).encode()


FEED = _feed(
    ("ep_new_0001", OWNER, "Lex Fridman", NOW - timedelta(days=1)),
    ("ep_guest_01", GUEST, "A Guest Channel", NOW - timedelta(days=3)),
)


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        PID,
        f"https://www.youtube.com/playlist?list={PID}",
        f"youtube.com/playlist?list={PID}",
        f"https://m.youtube.com/playlist?list={PID}&si=abc",
        f"https://music.youtube.com/playlist?list={PID}",
        f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PID}&index=3",
        f"https://youtu.be/dQw4w9WgXcQ?list={PID}",
        f"https://www.youtube.com/feeds/videos.xml?playlist_id={PID}",
    ],
)
def test_parse_playlist_references(value):
    assert playlists.parse_input(value) == PID


@pytest.mark.parametrize(
    "value",
    [
        OWNER,
        "@lexfridman",
        "lexfridman",
        "https://www.youtube.com/@lexfridman",
        f"https://www.youtube.com/channel/{OWNER}",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://nebula.tv/tomscott",
        f"https://example.com/playlist?list={PID}",
    ],
)
def test_parse_leaves_everything_else_alone(value):
    assert playlists.parse_input(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ",  # a mix
        "https://www.youtube.com/playlist?list=WL",
        "https://www.youtube.com/playlist?list=UUSHZKyawb77ixDdsGog4iWA",  # a channel's uploads
    ],
)
def test_parse_rejects_playlists_without_a_feed(value):
    with pytest.raises(ChannelLookupError):
        playlists.parse_input(value)


# --- lookup ----------------------------------------------------------------


@pytest.fixture
def upstream(monkeypatch):
    """Route playlists.py's httpx traffic to a dict of url -> (status, body)."""
    routes: dict[str, tuple[int, bytes]] = {}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(str(request.url), (404, b""))
        return httpx.Response(status, content=body)

    monkeypatch.setattr(
        playlists.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    return routes


def _page(title: str, description: str = "") -> bytes:
    return (
        f'<html><head><meta property="og:title" content="{title}">'
        f'<meta property="og:description" content="{description}"></head></html>'
    ).encode()


def test_resolve_reads_the_playlist_page(upstream):
    upstream[playlists.page_url(PID)] = (200, _page("Q&amp;A Episodes", "Questions &amp; answers"))
    r = playlists.resolve_playlist(f"https://www.youtube.com/playlist?list={PID}")
    assert (r.channel_id, r.platform) == (PID, "youtube_playlist")
    assert (r.title, r.description) == ("Q&A Episodes", "Questions & answers")


def test_resolve_missing_playlist(upstream):
    # YouTube answers 200 for a playlist that doesn't exist, titled "undefined".
    upstream[playlists.page_url(PID)] = (200, _page("undefined"))
    with pytest.raises(ChannelLookupError):
        playlists.resolve_playlist(PID)


def test_resolve_falls_back_to_the_feed(upstream):
    upstream[playlists.page_url(PID)] = (500, b"")
    upstream[playlists.feed_url(PID)] = (200, FEED)
    assert playlists.resolve_playlist(PID).title == "Lex Fridman Podcast"


# --- database-backed behaviour -----------------------------------------------


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # Enforced like Postgres would, so a video inserted ahead of its uploader's
    # stub channel row fails here too.
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        # Shorts excluded and every archive feature on at the account level,
        # so anything leaking through to a playlist shows up.
        s.add(
            User(
                id=1,
                oidc_sub="s",
                email="e",
                include_shorts=False,
                download_enabled=True,
                generate_podcast=True,
            )
        )
        s.add(Channel(channel_id=PID, platform="youtube_playlist", title="Lex Fridman Podcast"))
        s.commit()
        s.add(OpmlToken(user_id=1, token=TOKEN))
        s.add(Subscription(user_id=1, channel_id=PID))
        s.add(Category(id=1, user_id=1, name="Podcasts", slug="podcasts", download_enabled=True))
        s.commit()
        s.add(ChannelCategory(user_id=1, channel_id=PID, category_id=1))
        s.commit()
    return factory


@pytest.fixture
def db(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def no_settings(monkeypatch):
    monkeypatch.setattr(
        poller,
        "get_settings",
        lambda: SimpleNamespace(poll_max_retries=0, poll_channel_delay_seconds=0, youtube_api_key=""),
    )
    poller._api_fallback_at.clear()


def _client(*bodies: bytes) -> tuple[httpx.Client, list[str]]:
    calls: list[str] = []
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=queue.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


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


def test_poll_records_videos_under_their_uploaders(db, no_settings):
    client, calls = _client(FEED)
    assert poller.poll_channel(PID, db, client) == 2
    db.commit()

    assert calls == [playlists.feed_url(PID)]
    assert db.get(ChannelFeedCache, PID).xml == FEED
    videos = {v.video_id: v for v in db.execute(select(Video)).scalars()}
    assert {vid: v.channel_id for vid, v in videos.items()} == {"ep_new_0001": OWNER, "ep_guest_01": GUEST}
    # New to us from a playlist: kept out of anyone's archive of the uploader.
    assert all(v.backfilled for v in videos.values())
    # Uploaders nobody follows get a stub row, titled from the feed.
    assert db.get(Channel, GUEST).title == "A Guest Channel"
    assert db.get(Channel, GUEST).platform == "youtube"
    members = db.execute(select(PlaylistVideo.video_id).where(PlaylistVideo.playlist_id == PID)).scalars()
    assert set(members) == {"ep_new_0001", "ep_guest_01"}


def test_repoll_adds_only_whats_new_to_the_playlist(db, no_settings):
    newer = _feed(("ep_newest01", OWNER, "Lex Fridman", NOW))
    client, _ = _client(FEED, FEED, newer)
    poller.poll_channel(PID, db, client)
    db.commit()
    assert poller.poll_channel(PID, db, client) == 0
    db.commit()
    assert poller.poll_channel(PID, db, client) == 1
    db.commit()
    # Dropping off the feed doesn't take a video out of the playlist.
    assert len(db.execute(select(PlaylistVideo)).scalars().all()) == 3


def test_poll_leaves_a_followed_channels_videos_as_they_were(db, no_settings):
    db.add(Channel(channel_id=OWNER, title="Lex Fridman"))
    db.commit()
    db.add(Video(video_id="ep_new_0001", channel_id=OWNER, title="From the channel feed", backfilled=False))
    db.commit()

    client, _ = _client(FEED)
    poller.poll_channel(PID, db, client)
    db.commit()

    video = db.get(Video, "ep_new_0001")
    assert (video.title, video.backfilled) == ("From the channel feed", False)
    assert db.get(PlaylistVideo, (PID, "ep_new_0001")) is not None


def test_playlists_are_never_dormant(db, no_settings):
    # No videos of its own to judge an upload rate by: due every sweep.
    assert poller._due_channels(db, [PID], timedelta(minutes=20)) == [PID]


def test_failed_playlist_feed_falls_back_to_the_playlist_items(db, monkeypatch):
    monkeypatch.setattr(
        poller,
        "get_settings",
        lambda: SimpleNamespace(
            poll_max_retries=0,
            poll_channel_delay_seconds=0,
            youtube_api_key="key",
            youtube_api_fallback_interval_minutes=60,
        ),
    )
    poller._api_fallback_at.clear()
    item = {
        "snippet": {
            "publishedAt": "2026-10-27T10:00:00Z",
            "title": "From the API",
            "resourceId": {"videoId": "ep_api_0001"},
            "videoOwnerChannelId": GUEST,
            "videoOwnerChannelTitle": "A Guest Channel",
        },
        "contentDetails": {"videoPublishedAt": "2026-10-27T09:00:00Z"},
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "www.youtube.com":
            return httpx.Response(404)
        return httpx.Response(200, json={"items": [item]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert poller.poll_channel(PID, db, client) == 1
    db.commit()

    assert calls == [playlists.feed_url(PID), uploads_api.playlist_url(PID)]
    assert db.get(Video, "ep_api_0001").channel_id == GUEST
    assert db.get(Channel, GUEST).title == "A Guest Channel"


def test_playlist_feed_rendering_names_each_uploader():
    xml = uploads_api.build_playlist_feed(
        PID,
        [
            {
                "snippet": {
                    "publishedAt": "2026-10-27T10:00:00Z",
                    "resourceId": {"videoId": "ep_api_0001"},
                    "videoOwnerChannelId": GUEST,
                    "videoOwnerChannelTitle": "A Guest Channel",
                }
            },
            # Deleted or private: no owner, skipped as in an uploads feed.
            {"snippet": {"publishedAt": "2026-10-27T10:00:00Z", "resourceId": {"videoId": "ep_gone_001"}}},
        ],
        title="Lex Fridman Podcast",
    )
    entries = poller._parse_entries(xml)
    assert [(e.video_id, e.channel_id, e.author) for e in entries] == [("ep_api_0001", GUEST, "A Guest Channel")]


def test_playlists_are_never_archived(db, no_settings):
    client, _ = _client(FEED)
    poller.poll_channel(PID, db, client)
    db.commit()
    assert archive.channel_intents(db) == {}
    assert archive.enqueue_pending(db) == 0


def test_feed_proxy_filters_a_playlist_like_a_channel(session_factory):
    with session_factory() as s:
        s.add(ChannelFeedCache(channel_id=PID, xml=FEED))
        s.add(VideoShort(video_id="ep_guest_01", is_short=True))
        s.commit()
    resp = _app(session_factory, feed.router).get(f"/feed/{TOKEN}/{PID}.xml")
    assert resp.status_code == 200
    assert b"ep_new_0001" in resp.content
    assert b"ep_guest_01" not in resp.content  # a Short, and Shorts are excluded


def test_opml_links_the_playlist_page(session_factory, monkeypatch):
    monkeypatch.setattr(opml, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    body = _app(session_factory, opml.router).get(f"/opml/{TOKEN}/all.opml").text
    assert f'xmlUrl="http://test/feed/{TOKEN}/{PID}.xml"' in body
    assert f'htmlUrl="https://www.youtube.com/playlist?list={PID}"' in body


def test_watch_tab_shows_playlist_videos_under_the_playlist(db, no_settings):
    client, _ = _client(FEED)
    poller.poll_channel(PID, db, client)
    db.commit()

    data = build_watch_feed(db.get_one(User, 1), db, now=NOW)
    assert [(v["key"], v["channelId"], v["cats"]) for v in data["videos"]] == [
        ("ep_new_0001", PID, ["c1"]),
        ("ep_guest_01", PID, ["c1"]),
    ]
    assert data["categories"][0]["channels"] == [{"id": PID, "title": "Lex Fridman Podcast", "nebula": False}]

    page = channel_videos(db.get_one(User, 1), db, db.get_one(Channel, PID))
    assert [v["key"] for v in page] == ["ep_new_0001", "ep_guest_01"]
    # Matched by the playlist's name, as shown, not the uploader's.
    assert [v["key"] for v in search_videos(db.get_one(User, 1), db, "fridman podcast")] == [
        "ep_new_0001",
        "ep_guest_01",
    ]


def test_watch_tab_lists_a_video_once_when_its_channel_is_followed_too(db, no_settings):
    db.add(Channel(channel_id=OWNER, title="Lex Fridman"))
    db.add(Subscription(user_id=1, channel_id=OWNER))
    db.commit()
    client, _ = _client(FEED)
    poller.poll_channel(PID, db, client)
    db.commit()

    data = build_watch_feed(db.get_one(User, 1), db, now=NOW)
    assert [(v["key"], v["channelId"]) for v in data["videos"]] == [
        ("ep_new_0001", OWNER),
        ("ep_guest_01", PID),
    ]


def test_detail_pane_for_a_playlist(session_factory, monkeypatch):
    monkeypatch.setattr(channels, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    tc = _app(session_factory, channels.router)
    body = tc.get(f"/channels/{PID}/detail", headers={"hx-request": "true"}).text
    assert ">Playlist<" in body
    assert f"https://www.youtube.com/playlist?list={PID}" in body
    assert "Not available for playlists yet" in body
    assert "Include Shorts" in body  # the feed filters still apply

    listing = tc.get("/channels", headers={"hx-request": "true"}).text
    assert 'title="YouTube playlist">PL<' in listing
    assert "ARC" not in listing  # downloads are on by default, but not for this


def test_add_playlist(session_factory, monkeypatch, upstream):
    monkeypatch.setattr(channels, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    warmed: list[bool] = []
    monkeypatch.setattr(scheduler, "warm_new_channels_soon", lambda: warmed.append(True))

    def no_channel_lookup(value):
        raise AssertionError("a playlist URL must not reach the channel resolver")

    monkeypatch.setattr(channels, "resolve_channel_public", no_channel_lookup)
    other = "PLotherotherotherother01"
    upstream[playlists.page_url(other)] = (200, _page("Short Stories"))

    tc = _app(session_factory, channels.router)
    resp = tc.post("/channels/add", data={"channel_input": f"https://www.youtube.com/watch?v=x&list={other}"})
    assert resp.status_code == 200
    assert "Short Stories" in resp.text
    assert warmed == [True]

    with session_factory() as s:
        ch = s.get_one(Channel, other)
        assert (ch.platform, ch.title) == ("youtube_playlist", "Short Stories")
        sub = s.get_one(Subscription, (1, other))
        assert sub.account_id is None


def test_add_rejects_a_mix(session_factory, monkeypatch):
    monkeypatch.setattr(channels, "get_settings", lambda: SimpleNamespace(base_url="http://test"))
    tc = _app(session_factory, channels.router)
    resp = tc.post("/channels/add", data={"channel_input": "https://www.youtube.com/watch?v=x&list=RDx"})
    assert resp.status_code == 400
    assert "PL" in resp.json()["detail"]
