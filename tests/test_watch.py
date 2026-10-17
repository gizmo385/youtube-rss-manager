"""Watch tab: feed assembly from stored videos/feeds, and watched state."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
    ChannelFeedCache,
    Download,
    DownloadLink,
    JellyfinAccount,
    Subscription,
    User,
    Video,
    VideoShort,
    WatchedVideo,
)
from youtube_subs_opml.web.routes import watch
from youtube_subs_opml.web.services.watch import build_watch_feed, set_watched

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

NEBULA_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Tom Scott</title>
<item><title>Recent</title><link>https://nebula.tv/videos/tomscott-recent/</link>
<description>&lt;img src=https://images.nebula.tv/0cb7be0d-35b9-48af-baf5-ece0e33cd6e6?format=jpeg&amp;width=1920&gt;</description>
<pubDate>Tue, 13 Oct 2026 15:00:00 +0000</pubDate></item>
<item><title>Ancient</title><link>https://nebula.tv/videos/tomscott-ancient/</link>
<pubDate>Mon, 01 Jun 2026 15:00:00 +0000</pubDate></item>
</channel></rss>"""


@pytest.fixture
def sessionmaker_():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    with TestingSession() as s:
        s.add(User(id=1, oidc_sub="s1", email="e1", include_shorts=False))
        s.add(User(id=2, oidc_sub="s2", email="e2"))
        s.add(Channel(channel_id="UCa", title="Alpha"))
        s.add(Channel(channel_id="UCb", title="Bravo"))
        s.add(Channel(channel_id="UCi", title="Ignored"))
        s.add(Channel(channel_id="nebula:tomscott", title="Tom Scott", platform="nebula"))
        s.commit()
        s.add(Category(id=7, user_id=1, name="Science", slug="science"))
        s.add(Category(id=8, user_id=1, name="Making", slug="making", include_shorts=True))
        s.add(Subscription(user_id=1, channel_id="UCa"))
        s.add(Subscription(user_id=1, channel_id="UCb"))
        s.add(Subscription(user_id=1, channel_id="UCi", ignored=True))
        s.add(Subscription(user_id=1, channel_id="nebula:tomscott"))
        s.commit()
        # Alpha is in both categories; Bravo and Tom Scott are uncategorized.
        s.add(ChannelCategory(user_id=1, channel_id="UCa", category_id=7))
        s.add(ChannelCategory(user_id=1, channel_id="UCa", category_id=8))
        s.add_all(
            [
                Video(video_id="a_new", channel_id="UCa", title="Alpha new", published_at=NOW - timedelta(hours=2)),
                Video(video_id="a_short", channel_id="UCa", title="Alpha short", published_at=NOW - timedelta(hours=3)),
                Video(video_id="a_old", channel_id="UCa", title="Too old", published_at=NOW - timedelta(days=45)),
                Video(
                    video_id="b_arch",
                    channel_id="UCb",
                    title="Bravo archived",
                    published_at=NOW - timedelta(days=1),
                    duration_seconds=3725,
                ),
                Video(video_id="i_vid", channel_id="UCi", title="Ignored vid", published_at=NOW - timedelta(hours=1)),
            ]
        )
        s.add(VideoShort(video_id="a_short", is_short=True))
        s.add(ChannelFeedCache(channel_id="nebula:tomscott", xml=NEBULA_FEED))
        s.add(JellyfinAccount(user_id=1, base_url="https://jf.example.com", api_key_encrypted=b"x"))
        s.commit()
        s.add(Download(video_id="b_arch", status="complete"))
        s.commit()
        s.add(DownloadLink(user_id=1, video_id="b_arch", jellyfin_item_id="item1"))
        s.add(WatchedVideo(user_id=1, video_key="a_new"))
        s.add(WatchedVideo(user_id=2, video_key="b_arch"))
        s.commit()
    return TestingSession


def _feed(sm) -> dict:
    with sm() as s:
        return build_watch_feed(s.get_one(User, 1), s, now=NOW)


def test_feed_collects_recent_videos_newest_first(sessionmaker_):
    feed = _feed(sessionmaker_)
    keys = [v["key"] for v in feed["videos"]]
    # Ignored channels and anything outside the window are left out; Nebula
    # videos come from the cached feed.
    assert keys == ["a_new", "a_short", "b_arch", "nebula:tomscott-recent"]


def test_channel_in_two_categories_appears_in_both(sessionmaker_):
    feed = _feed(sessionmaker_)
    by_key = {v["key"]: v for v in feed["videos"]}
    assert by_key["a_new"]["cats"] == ["c7", "c8"]
    assert by_key["b_arch"]["cats"] == ["unc"]
    assert [c["id"] for c in feed["categories"]] == ["c8", "c7", "unc"]  # by name, Uncategorized last


def test_known_shorts_follow_the_category_cascade(sessionmaker_):
    # The user hides Shorts, but Making opts back in — so the Short shows there only.
    by_key = {v["key"]: v for v in _feed(sessionmaker_)["videos"]}
    assert by_key["a_short"]["cats"] == ["c8"]


def test_archived_video_links_to_jellyfin(sessionmaker_):
    by_key = {v["key"]: v for v in _feed(sessionmaker_)["videos"]}
    assert by_key["b_arch"]["jellyfin"] is True
    assert by_key["b_arch"]["archived"] is True
    assert by_key["a_new"]["archived"] is False
    assert by_key["b_arch"]["url"] == "https://jf.example.com/web/#/details?id=item1"
    assert by_key["b_arch"]["duration"] == 3725
    assert by_key["a_new"]["url"] == "https://www.youtube.com/watch?v=a_new"


def test_nebula_video_shape(sessionmaker_):
    by_key = {v["key"]: v for v in _feed(sessionmaker_)["videos"]}
    neb = by_key["nebula:tomscott-recent"]
    assert neb["nebula"] is True
    assert neb["url"] == "https://nebula.tv/videos/tomscott-recent/"
    assert neb["thumb"] == "https://images.nebula.tv/0cb7be0d-35b9-48af-baf5-ece0e33cd6e6?format=jpeg&width=640"


def test_watched_state_is_per_user(sessionmaker_):
    by_key = {v["key"]: v for v in _feed(sessionmaker_)["videos"]}
    assert by_key["a_new"]["watched"] is True
    # Another user watched b_arch; that doesn't leak.
    assert by_key["b_arch"]["watched"] is False


def test_set_watched_is_idempotent(sessionmaker_):
    with sessionmaker_() as s:
        user = s.get_one(User, 1)
        set_watched(user, s, ["a_new", "b_arch", "b_arch"], True)
        set_watched(user, s, ["a_new", "b_arch"], True)
        assert set(s.execute(select(WatchedVideo.video_key).where(WatchedVideo.user_id == 1)).scalars()) == {
            "a_new",
            "b_arch",
        }
        set_watched(user, s, ["a_new", "never_watched"], False)
        assert set(s.execute(select(WatchedVideo.video_key).where(WatchedVideo.user_id == 1)).scalars()) == {"b_arch"}
        # The other user's row is untouched.
        assert s.execute(select(WatchedVideo).where(WatchedVideo.user_id == 2)).scalar_one().video_key == "b_arch"


@pytest.fixture
def client(sessionmaker_, monkeypatch):
    # The route uses the real clock; widen the window so the fixed fixture
    # dates stay inside it however long from now this runs.
    monkeypatch.setattr("youtube_subs_opml.web.services.watch.WINDOW", timedelta(days=36500))

    def odb():
        db = sessionmaker_()
        try:
            yield db
        finally:
            db.close()

    def ou(db: Session = Depends(get_db)):
        return db.get(User, 1)

    app = FastAPI()
    app.include_router(watch.router)
    app.dependency_overrides[get_db] = odb
    app.dependency_overrides[get_current_user] = ou
    return TestClient(app)


def test_page_embeds_the_feed(client):
    html = client.get("/watch").text
    assert 'class="nav-link active">Watch<' in html
    match = re.search(r'<script id="watch-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert match
    data = json.loads(match.group(1))
    assert data["videos"][0]["key"] == "a_new"


def test_watched_endpoint_round_trips(client, sessionmaker_):
    assert client.post("/watch/watched", json={"keys": ["b_arch"], "watched": True}).status_code == 204
    with sessionmaker_() as s:
        assert s.get(WatchedVideo, (1, "b_arch")) is not None
    assert client.post("/watch/watched", json={"keys": ["b_arch"], "watched": False}).status_code == 204
    with sessionmaker_() as s:
        assert s.get(WatchedVideo, (1, "b_arch")) is None


def test_watched_endpoint_pushes_to_freshrss(client, monkeypatch):
    from youtube_subs_opml.web.services import scheduler

    pushed = []
    monkeypatch.setattr(scheduler, "sync_freshrss_soon", lambda user_id: pushed.append(user_id))
    client.post("/watch/watched", json={"keys": ["b_arch"], "watched": True})
    assert pushed == [1]


def test_watched_endpoint_rejects_bad_bodies(client):
    assert client.post("/watch/watched", json={"keys": "b_arch", "watched": True}).status_code == 422
    assert client.post("/watch/watched", json={"keys": ["x"] * 2001, "watched": True}).status_code == 422


def test_media_streams_archived_file_for_subscribers(client, sessionmaker_, tmp_path):
    f = tmp_path / "b_arch.mkv"
    f.write_bytes(b"0123456789")
    with sessionmaker_() as s:
        s.get_one(Download, "b_arch").file_path = str(f)
        s.commit()
    resp = client.get("/watch/media/b_arch", headers={"Range": "bytes=2-5"})
    assert resp.status_code == 206
    assert resp.content == b"2345"
    assert resp.headers["content-type"] == "video/x-matroska"


def test_media_404s_for_unarchived_or_missing_files(client, sessionmaker_):
    assert client.get("/watch/media/a_new").status_code == 404  # no download
    with sessionmaker_() as s:
        s.get_one(Download, "b_arch").file_path = "/nonexistent/b_arch.mkv"
        s.commit()
    assert client.get("/watch/media/b_arch").status_code == 404


def test_media_requires_a_subscription(client, sessionmaker_, tmp_path):
    f = tmp_path / "x.mkv"
    f.write_bytes(b"x")
    with sessionmaker_() as s:
        s.add(Channel(channel_id="UCz", title="Not mine"))
        s.add(Video(video_id="z_vid", channel_id="UCz", title="z", published_at=NOW))
        s.commit()
        s.add(Download(video_id="z_vid", status="complete", file_path=str(f)))
        s.commit()
    assert client.get("/watch/media/z_vid").status_code == 404


def test_played_marks_the_item_in_jellyfin(client, sessionmaker_, monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "decrypt_token", lambda _: "key")
    monkeypatch.setattr(watch.JellyfinClient, "mark_played", lambda self, item, user: calls.append((item, user)))
    with sessionmaker_() as s:
        s.execute(select(JellyfinAccount)).scalar_one().jellyfin_user_id = "jf-user"
        s.commit()
    assert client.post("/watch/played/b_arch").status_code == 204
    assert calls == [("item1", "jf-user")]
    # No Jellyfin item for this one.
    assert client.post("/watch/played/a_new").status_code == 404


def test_channel_videos_ignore_the_window_and_shorts_filter(sessionmaker_):
    from youtube_subs_opml.web.services.watch import channel_videos

    with sessionmaker_() as s:
        vids = channel_videos(s.get_one(User, 1), s, s.get_one(Channel, "UCa"))
    # The 45-day-old upload and the Short both show on the channel's own page.
    assert [v["key"] for v in vids] == ["a_new", "a_short", "a_old"]
    assert vids[0]["watched"] is True
    assert vids[0]["channel"] == "Alpha"


def test_channel_videos_cap(sessionmaker_, monkeypatch):
    from youtube_subs_opml.web.services import watch as watch_service

    monkeypatch.setattr(watch_service, "CHANNEL_VIDEO_LIMIT", 2)
    with sessionmaker_() as s:
        vids = watch_service.channel_videos(s.get_one(User, 1), s, s.get_one(Channel, "UCa"))
    assert [v["key"] for v in vids] == ["a_new", "a_short"]


def test_channel_videos_for_nebula_come_from_the_feed(sessionmaker_):
    from youtube_subs_opml.web.services.watch import channel_videos

    with sessionmaker_() as s:
        vids = channel_videos(s.get_one(User, 1), s, s.get_one(Channel, "nebula:tomscott"))
    assert [v["key"] for v in vids] == ["nebula:tomscott-recent", "nebula:tomscott-ancient"]
