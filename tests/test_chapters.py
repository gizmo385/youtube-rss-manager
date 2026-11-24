"""Chapters: reading them out of descriptions, yt-dlp's list from the probe,
the player's endpoint, and the poller keeping descriptions."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.downloader import ytdlp
from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import Channel, User, Video
from youtube_subs_opml.web.routes import watch
from youtube_subs_opml.web.services import poller
from youtube_subs_opml.web.services.chapters import Chapter, for_video, parse_description

DESCRIPTION = """Today we build a thing.

Sponsored by Example — first 100 people get 10% off.

Chapters:
0:00 Intro
1:30 - The problem
[4:05] Building it
Testing it 12:00
1:02:03 | Wrapping up

Music: someone, 3:33 into their set
"""


def test_parses_a_description_chapter_list():
    assert parse_description(DESCRIPTION) == [
        Chapter(0, "Intro"),
        Chapter(90, "The problem"),
        Chapter(245, "Building it"),
        Chapter(720, "Testing it"),
        Chapter(3723, "Wrapping up"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "No timestamps at all",
        # Doesn't start at 0:00.
        "0:30 a\n1:00 b\n2:00 c",
        # Fewer than three.
        "0:00 a\n1:00 b",
        # A chapter under ten seconds.
        "0:00 a\n0:05 b\n2:00 c",
    ],
)
def test_no_chapters_unless_youtube_would_show_them(text):
    assert parse_description(text) == []


def test_a_list_ends_at_the_first_timestamp_out_of_order():
    text = "0:00 a\n1:00 b\n2:00 c\n\nTracklist:\n0:30 song\n"
    assert [c.title for c in parse_description(text)] == ["a", "b", "c"]


def test_an_earlier_stray_timestamp_does_not_start_the_list():
    text = "The good part is at 5:00!\n\n00:00 a\n01:00 b\n02:00 c"
    assert [c.start for c in parse_description(text)] == [0, 60, 120]


def test_not_fooled_by_times_of_day_or_ratios():
    assert parse_description("0:00 a\n1:00 b\n2:00 c\nLive at 19:75\n") == [
        Chapter(0, "a"),
        Chapter(60, "b"),
        Chapter(120, "c"),
    ]


# --- probe and endpoint ---------------------------------------------------------


def test_probe_keeps_ytdlps_chapters(monkeypatch):
    data = {
        "duration": 600,
        "title": "t",
        "chapters": [
            {"start_time": 0.0, "end_time": 42.0, "title": "Key moment"},
            {"start_time": 42.0, "end_time": 600.0, "title": "Rest"},
        ],
    }

    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")

    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    meta = ytdlp.probe("vid")
    assert [c["title"] for c in meta.chapters] == ["Key moment", "Rest"]


def test_download_embeds_youtubes_chapters(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        (tmp_path / "ep.mkv").write_bytes(b"mkv")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    ytdlp.download("vid", tmp_path / "ep", video_format="best")
    assert "--embed-chapters" in calls[0]


@pytest.fixture
def sm():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    with Session_() as s:
        s.add(User(id=1, oidc_sub="s1", email="e1"))
        s.add(Channel(channel_id="UCa", title="Alpha"))
        s.commit()
        s.add_all(
            [
                Video(video_id="described", channel_id="UCa", title="v", description=DESCRIPTION),
                Video(
                    video_id="probed",
                    channel_id="UCa",
                    title="v",
                    description=DESCRIPTION,
                    chapters=[
                        {"start": 0, "end": 42.5, "title": "Auto 1"},
                        {"start": 42.5, "end": 99, "title": "Auto 2"},
                    ],
                ),
                Video(video_id="plain", channel_id="UCa", title="v", description="Nothing here"),
            ]
        )
        s.commit()
    return Session_


def test_probed_chapters_win_over_the_description(sm):
    with sm() as s:
        assert [c.title for c in for_video(s, "probed")] == ["Auto 1", "Auto 2"]
        assert len(for_video(s, "described")) == 5
        assert for_video(s, "plain") == []
        assert for_video(s, "unknown") == []


def test_chapters_endpoint(sm):
    def odb():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(watch.router)
    app.dependency_overrides[get_db] = odb
    app.dependency_overrides[get_current_user] = lambda db=Depends(get_db): db.get(User, 1)
    client = TestClient(app)

    got = client.get("/watch/chapters/described").json()["chapters"]
    assert got[1] == {"start": 90, "end": None, "title": "The problem"}
    assert client.get("/watch/chapters/probed").json()["chapters"][1] == {"start": 42.5, "end": 99, "title": "Auto 2"}
    assert client.get("/watch/chapters/plain").json() == {"chapters": []}


# --- the poller keeps descriptions -------------------------------------------------


def _feed(description: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/">'
        "<entry><yt:videoId>vidAAAA1111</yt:videoId><title>Hi</title>"
        "<published>2026-08-20T09:00:00+00:00</published>"
        f"<media:group><media:title>Hi</media:title><media:description>{description}</media:description></media:group>"
        "</entry></feed>"
    ).encode()


def test_poller_saves_and_updates_the_description(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setenv("POLL_CHANNEL_DELAY_SECONDS", "0")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    config.get_settings.cache_clear()
    monkeypatch.setattr(poller, "_rss_breaker", poller._RetryBreaker())

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    cid = "UCchannel00000000000001"
    responses = [_feed("First draft"), _feed("0:00 a\n1:00 b\n2:00 c")]

    class Client:
        def __init__(self):
            self.headers: dict[str, str] = {}

        def get(self, u, headers=None):
            return httpx.Response(200, content=responses.pop(0), request=httpx.Request("GET", u))

        def close(self):
            pass

    monkeypatch.setattr(poller, "_new_client", Client)
    try:
        poller.poll_channel(cid, db)
        db.commit()
        assert db.get_one(Video, "vidAAAA1111").description == "First draft"

        # The creator added chapters after upload.
        poller.poll_channel(cid, db)
        db.commit()
        db.expire_all()
        assert len(for_video(db, "vidAAAA1111")) == 3
    finally:
        db.close()
        config.get_settings.cache_clear()


# --- the downloader's second look -------------------------------------------------


def test_recheck_probes_videos_archived_before_youtube_had_chapters(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from youtube_subs_opml.downloader import worker

    now = datetime(2026, 11, 24, 12, tzinfo=UTC)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)

    def video(vid, age, checked_after=timedelta(minutes=5), **kw):
        published = now - age
        kw.setdefault("chapters", [])
        return Video(
            video_id=vid,
            channel_id="UCa",
            title=vid,
            published_at=published,
            chapters_checked_at=published + checked_after if checked_after is not None else None,
            description="Nothing here",
            **kw,
        )

    with Session_() as s:
        s.add(Channel(channel_id="UCa", title="Alpha"))
        s.commit()
        s.add_all(
            [
                # Probed minutes after upload, now 7h old: looked at again.
                video("early", timedelta(hours=7)),
                # Probed before the timestamp was kept: looked at again too.
                video("unknown", timedelta(hours=8), checked_after=None),
                # Not old enough yet.
                video("young", timedelta(hours=2)),
                # Too old to bother with.
                video("old", timedelta(days=5)),
                # Already looked at once it was old enough.
                video("rechecked", timedelta(hours=9), checked_after=timedelta(hours=7)),
                # Already has chapters, from the probe or the description.
                video("probed", timedelta(hours=7), chapters=[{"start": 0, "end": 5, "title": "a"}]),
                video("described", timedelta(hours=7), checked_after=timedelta(minutes=5)),
                # Never probed (not archived).
                video("unarchived", timedelta(hours=7), chapters=None),
            ]
        )
        s.commit()
        described = s.get(Video, "described")
        assert described is not None
        described.description = DESCRIPTION
        s.commit()

    probed = []

    def probe(vid):
        probed.append(vid)
        if vid == "unknown":
            raise ytdlp.ProbeError("gone")
        return ytdlp.VideoMetadata(
            duration_seconds=600,
            title=vid,
            description="Still nothing",
            uploader="",
            thumbnail_url=None,
            chapters=[{"start_time": 0.0, "end_time": 42.0, "title": "Key moment"}],
        )

    monkeypatch.setattr(worker.ytdlp, "probe", probe)
    with Session_() as s:
        found = worker.recheck_chapters(s, now=now)
    assert sorted(probed) == ["early", "unknown"]
    assert found == 1

    with Session_() as s:
        early, unknown = s.get(Video, "early"), s.get(Video, "unknown")
        assert early is not None and unknown is not None
        assert early.chapters == [{"start": 0.0, "end": 42.0, "title": "Key moment"}]
        assert early.description == "Still nothing"
        # A failed probe isn't retried, and neither is one that worked.
        assert unknown.chapters == []
        assert unknown.chapters_checked_at is not None

    probed.clear()
    with Session_() as s:
        assert worker.recheck_chapters(s, now=now + timedelta(hours=1)) == 0
    assert probed == []
