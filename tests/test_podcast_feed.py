"""Podcast RSS feed generation and range-capable media serving.

In-memory SQLite; the audio enclosure is backed by a real temp file so the
range request exercises Starlette's FileResponse for real.
"""
from __future__ import annotations

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
    Download,
    OpmlToken,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.routes import podcast

TOKEN = "podtoken123"
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CID = "UCchannelaaaaaaaaaaaaaa"
CID2 = "UCchannelbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def audio_file(tmp_path):
    p = tmp_path / "episode.m4a"
    p.write_bytes(b"AUDIODATA-0123456789")  # 20 bytes
    return p


@pytest.fixture
def client(audio_file):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            t.__table__
            for t in (
                User, Channel, Subscription, Category, ChannelCategory,
                OpmlToken, Video, Download,
            )
        ],
    )
    TestingSession = sessionmaker(bind=engine)

    with TestingSession() as seed:
        seed.add(User(id=1, oidc_sub="s", email="e", display_name="Tester"))
        seed.add(OpmlToken(user_id=1, token=TOKEN))
        seed.add(Channel(channel_id=CID, title="Tech Channel"))
        seed.add(Channel(channel_id=CID2, title="Other Channel"))
        seed.add(Subscription(user_id=1, channel_id=CID))
        seed.add(Subscription(user_id=1, channel_id=CID2))
        seed.add(Category(id=1, user_id=1, name="Tech", slug="tech"))
        seed.add(ChannelCategory(user_id=1, channel_id=CID, category_id=1))

        pub = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
        # Complete + audio -> appears in feed.
        seed.add(Video(video_id="withaudio001", channel_id=CID, title="Has Audio",
                       published_at=pub, duration_seconds=3661))
        seed.add(Download(video_id="withaudio001", status="complete",
                          file_path="/c/withaudio001.mkv",
                          audio_path=str(audio_file), audio_size_bytes=20))
        # Complete but no audio -> excluded.
        seed.add(Video(video_id="noaudio00001", channel_id=CID, title="No Audio",
                       published_at=pub))
        seed.add(Download(video_id="noaudio00001", status="complete",
                          file_path="/c/noaudio.mkv"))
        # Different channel, in no category -> only in the all feed.
        seed.add(Video(video_id="otheraud0001", channel_id=CID2, title="Other Audio",
                       published_at=pub, duration_seconds=125))
        seed.add(Download(video_id="otheraud0001", status="complete",
                          file_path="/c/other.mkv",
                          audio_path=str(audio_file), audio_size_bytes=20))
        seed.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(podcast.router)
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _items(xml: bytes):
    root = ET.fromstring(xml)
    return root.find("channel").findall("item")


def test_all_feed_lists_only_complete_with_audio(client):
    resp = client.get(f"/podcast/{TOKEN}/all.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")

    items = _items(resp.content)
    guids = {it.find("guid").text for it in items}
    assert guids == {"withaudio001", "otheraud0001"}  # noaudio excluded


def test_feed_item_shape(client):
    resp = client.get(f"/podcast/{TOKEN}/all.xml")
    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    # channel-level iTunes bits Apple wants
    assert channel.find("language").text == "en"
    assert channel.find(f"{{{ITUNES}}}explicit").text == "false"

    by_guid = {it.find("guid").text: it for it in channel.findall("item")}
    it = by_guid["withaudio001"]
    enc = it.find("enclosure")
    assert enc.get("url").endswith(f"/media/{TOKEN}/withaudio001.m4a")
    assert enc.get("length") == "20"           # real byte count, not estimated
    assert enc.get("type") == "audio/x-m4a"
    assert it.find(f"{{{ITUNES}}}duration").text == "1:01:01"  # 3661s
    assert it.find("guid").get("isPermaLink") == "false"
    assert it.find("pubDate") is not None


def test_category_feed_scopes_to_category(client):
    resp = client.get(f"/podcast/{TOKEN}/tech.xml")
    assert resp.status_code == 200
    guids = {it.find("guid").text for it in _items(resp.content)}
    assert guids == {"withaudio001"}  # otheraud is not in 'tech'


def test_unknown_token_404(client):
    assert client.get(f"/podcast/bad/all.xml").status_code == 404


def test_unknown_category_404(client):
    assert client.get(f"/podcast/{TOKEN}/nope.xml").status_code == 404


def test_media_serves_full_and_range(client):
    full = client.get(f"/media/{TOKEN}/withaudio001.m4a")
    assert full.status_code == 200
    assert full.content == b"AUDIODATA-0123456789"
    assert full.headers.get("content-type") == "audio/x-m4a"

    ranged = client.get(
        f"/media/{TOKEN}/withaudio001.m4a", headers={"Range": "bytes=0-3"}
    )
    assert ranged.status_code == 206
    assert ranged.content == b"AUDI"
    assert ranged.headers["content-range"] == "bytes 0-3/20"


def test_media_404_without_audio(client):
    assert client.get(f"/media/{TOKEN}/noaudio00001.m4a").status_code == 404


def test_media_404_when_not_subscribed(client):
    assert client.get(f"/media/{TOKEN}/UCnope.m4a").status_code == 404


def test_media_404_when_file_missing(client, audio_file):
    audio_file.unlink()
    assert client.get(f"/media/{TOKEN}/withaudio001.m4a").status_code == 404
