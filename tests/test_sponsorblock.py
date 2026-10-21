"""SponsorBlock: the preference cascade, segment lookups, the player's
segments endpoint, and chapter marking in the downloader."""

from __future__ import annotations

import hashlib

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
from youtube_subs_opml.web.models import Category, Channel, ChannelCategory, Subscription, User, Video
from youtube_subs_opml.web.routes import watch
from youtube_subs_opml.web.services import sponsorblock
from youtube_subs_opml.web.services.archive import channel_intents

VID = "dQw4w9WgXcQ"


@pytest.fixture
def sm():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    with Session_() as s:
        s.add_all([User(id=1, oidc_sub="s1", email="e1"), User(id=2, oidc_sub="s2", email="e2")])
        s.add(Channel(channel_id="UCa", title="Alpha"))
        s.commit()
        s.add_all([Subscription(user_id=1, channel_id="UCa"), Subscription(user_id=2, channel_id="UCa")])
        s.add(Category(id=7, user_id=1, name="Science", slug="science"))
        s.add(Category(id=8, user_id=1, name="Making", slug="making"))
        s.add(Video(video_id=VID, channel_id="UCa", title="v"))
        s.commit()
        s.add_all(
            [
                ChannelCategory(user_id=1, channel_id="UCa", category_id=7),
                ChannelCategory(user_id=1, channel_id="UCa", category_id=8),
            ]
        )
        s.commit()
    return Session_


@pytest.fixture(autouse=True)
def _empty_cache():
    sponsorblock._cache.clear()
    yield
    sponsorblock._cache.clear()


def _set(sm, model, key, **values):
    with sm() as s:
        row = s.get_one(model, key)
        for k, v in values.items():
            setattr(row, k, v)
        s.commit()


def _mode(sm, user_id=1) -> str:
    with sm() as s:
        return sponsorblock.resolve_mode(s, s.get_one(User, user_id), "UCa")


# --- preferences -------------------------------------------------------------


def test_parsing():
    assert sponsorblock.parse_mode("skip", allow_inherit=True) == "skip"
    assert sponsorblock.parse_mode("inherit", allow_inherit=True) is None
    assert sponsorblock.parse_mode("bogus", allow_inherit=False) == "off"
    assert sponsorblock.parse_categories(["filler", "sponsor", "nope"]) == "sponsor,filler"


def test_mode_cascades_subscription_category_user(sm):
    assert _mode(sm) == "off"  # the user default
    _set(sm, User, 1, sponsorblock="show")
    assert _mode(sm) == "show"
    _set(sm, Category, 7, sponsorblock="off")
    assert _mode(sm) == "off"  # a category overrides the user
    _set(sm, Category, 8, sponsorblock="skip")
    assert _mode(sm) == "skip"  # categories that disagree: the strongest wins
    _set(sm, Subscription, (1, "UCa"), sponsorblock="show")
    assert _mode(sm) == "show"  # the channel overrides its categories
    assert _mode(sm, user_id=2) == "off"  # and none of it leaks to another user


def _podcast_cut(sm, user_id=1) -> str | None:
    with sm() as s:
        return sponsorblock.podcast_cut(s, s.get_one(User, user_id), "UCa")


def test_podcast_mode_cascades_independently_of_the_players(sm):
    _set(sm, User, 1, sponsorblock="skip", sponsorblock_categories="intro")
    assert _podcast_cut(sm) is None  # the player's setting doesn't reach podcasts
    _set(sm, User, 1, podcast_sponsorblock="remove", podcast_sponsorblock_categories="sponsor,outro")
    assert _podcast_cut(sm) == "sponsor,outro"
    _set(sm, Category, 7, podcast_sponsorblock="off")
    assert _podcast_cut(sm) is None  # a category overrides the user
    _set(sm, Category, 8, podcast_sponsorblock="remove")
    assert _podcast_cut(sm) == "sponsor,outro"  # categories that disagree: remove wins
    _set(sm, Subscription, (1, "UCa"), podcast_sponsorblock="off")
    assert _podcast_cut(sm) is None  # the channel overrides its categories
    _set(sm, Subscription, (1, "UCa"), podcast_sponsorblock="remove")
    _set(sm, User, 1, podcast_sponsorblock_categories="")
    assert _podcast_cut(sm) is None  # no kinds chosen, nothing to cut
    with sm() as s:
        assert sponsorblock.resolve_mode(s, s.get_one(User, 1), "UCa") == "skip"


def test_podcast_mode_parsing():
    modes = sponsorblock.PODCAST_MODES
    assert sponsorblock.parse_mode("remove", allow_inherit=True, modes=modes) == "remove"
    assert sponsorblock.parse_mode("skip", allow_inherit=True, modes=modes) is None
    assert sponsorblock.parse_mode("show", allow_inherit=False, modes=modes) == "off"


# --- segment lookups ---------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _api_response(segments, video_id=VID):
    return [
        {"videoID": "someOtherId", "segments": [{"segment": [0, 5], "category": "sponsor", "actionType": "skip"}]},
        {"videoID": video_id, "segments": segments},
    ]


def test_segments_by_hash_prefix_filtered_and_sorted():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200,
            json=_api_response(
                [
                    {"segment": [300.5, 330], "category": "selfpromo", "actionType": "skip"},
                    {"segment": [60, 90.25], "category": "sponsor", "actionType": "skip"},
                    {"segment": [120, 125], "category": "intro", "actionType": "skip"},
                ]
            ),
        )

    with _client(handler) as client:
        found = sponsorblock.segments(VID, ["sponsor", "selfpromo"], client=client)
    assert [(s.start, s.end, s.category) for s in found] == [(60, 90.25, "sponsor"), (300.5, 330, "selfpromo")]
    # Only a prefix of the id's hash goes out, never the id.
    assert seen[0].path == f"/api/skipSegments/{hashlib.sha256(VID.encode()).hexdigest()[:4]}"
    assert VID not in str(seen[0])


def test_segments_are_cached_across_category_choices():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200, json=_api_response([{"segment": [1, 20], "category": "intro", "actionType": "skip"}])
        )

    with _client(handler) as client:
        assert sponsorblock.segments(VID, ["sponsor"], client=client) == []
        assert len(sponsorblock.segments(VID, ["intro"], client=client)) == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404), httpx.Response(503), httpx.Response(200, content=b"not json")],
)
def test_segments_fail_soft(response):
    with _client(lambda request: response) as client:
        assert sponsorblock.segments(VID, list(sponsorblock.CATEGORIES), client=client) == []


def test_lookup_tells_nothing_submitted_from_unreachable():
    with _client(lambda request: httpx.Response(404)) as client:
        assert sponsorblock.lookup(VID, client=client) == []
    sponsorblock._cache.clear()
    with _client(lambda request: httpx.Response(503)) as client:
        assert sponsorblock.lookup(VID, client=client) is None


# --- the player's endpoint -----------------------------------------------------


@pytest.fixture
def client(sm, monkeypatch):
    fetched = []

    def fake_segments(video_id, categories):
        fetched.append((video_id, categories))
        return [sponsorblock.Segment(60, 90, "sponsor")]

    monkeypatch.setattr(sponsorblock, "segments", fake_segments)

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
    c = TestClient(app)
    c.fetched = fetched  # type: ignore[attr-defined]
    return c


def test_segments_endpoint_off_skips_the_lookup(client):
    assert client.get(f"/watch/segments/{VID}").json() == {"mode": "off", "segments": []}
    assert client.get("/watch/segments/unknownvid1").json()["mode"] == "off"
    assert client.fetched == []


def test_segments_endpoint_returns_the_users_kinds(client, sm):
    _set(sm, User, 1, sponsorblock="skip", sponsorblock_categories="sponsor,outro")
    data = client.get(f"/watch/segments/{VID}").json()
    assert data == {"mode": "skip", "segments": [{"start": 60, "end": 90, "category": "sponsor"}]}
    assert client.fetched == [(VID, ["sponsor", "outro"])]


# --- archive: chapters for anyone who wants them -----------------------------


def test_intent_marks_chapters_when_a_downloading_subscriber_wants_sponsorblock(sm):
    with sm() as s:
        assert channel_intents(s)["UCa"].sponsorblock is False
    _set(sm, User, 2, sponsorblock="show")  # wants it, but doesn't archive
    with sm() as s:
        assert channel_intents(s)["UCa"].sponsorblock is False
    _set(sm, User, 2, download_enabled=True)
    with sm() as s:
        assert channel_intents(s)["UCa"].sponsorblock is True


def _fake_run(target, results):
    calls = []

    class _Result:
        def __init__(self, returncode, stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, "", stderr

    def run(cmd, **kwargs):
        calls.append(cmd)
        code, stderr = results[len(calls) - 1]
        if code == 0:
            target.with_suffix(".mkv").write_bytes(b"mkv")
        return _Result(code, stderr)

    return run, calls


def test_download_marks_sponsorblock_chapters(tmp_path, monkeypatch):
    target = tmp_path / "ep"
    run, calls = _fake_run(target, [(0, "")])
    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    ytdlp.download(VID, target, video_format="best", sponsorblock=True)
    cmd = calls[0]
    assert cmd[cmd.index("--sponsorblock-mark") + 1] == "all"
    assert "--sponsorblock-remove" not in cmd

    run, calls = _fake_run(target, [(0, "")])
    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    ytdlp.download(VID, target, video_format="best")
    assert "--sponsorblock-mark" not in calls[0]


def test_download_survives_sponsorblock_being_down(tmp_path, monkeypatch):
    target = tmp_path / "ep"
    run, calls = _fake_run(target, [(1, "ERROR: Postprocessing: Unable to communicate with SponsorBlock API"), (0, "")])
    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    assert ytdlp.download(VID, target, video_format="best", sponsorblock=True) == target.with_suffix(".mkv")
    assert len(calls) == 2 and "--sponsorblock-mark" not in calls[1]


def test_download_other_failures_still_fail(tmp_path, monkeypatch):
    target = tmp_path / "ep"
    run, calls = _fake_run(target, [(1, "ERROR: Video unavailable")])
    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    with pytest.raises(ytdlp.DownloadError, match="unavailable"):
        ytdlp.download(VID, target, video_format="best", sponsorblock=True)
    assert len(calls) == 1
