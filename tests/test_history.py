"""History backfill: paging back through channels' uploads for search, within
a quota budget, without the archive picking any of it up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.config import Settings
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import Channel, Download, Subscription, User, Video
from youtube_subs_opml.web.services import history
from youtube_subs_opml.web.services.archive import enqueue_pending

CID = "UCaaaaaaaaaaaaaaaaaaaaaa"
CID2 = "UCbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _fresh_budget():
    history._spent.clear()
    history._exhausted.clear()
    yield
    history._spent.clear()
    history._exhausted.clear()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, oidc_sub="s", email="e"))
    session.add_all([Channel(channel_id=CID, title="A"), Channel(channel_id=CID2, title="B")])
    session.add(Channel(channel_id="nebula:x", title="N", platform="nebula"))
    session.add(Channel(channel_id="UCcccccccccccccccccccccc", title="Unsubscribed"))
    session.commit()
    session.add_all(
        [
            Subscription(user_id=1, channel_id=CID),
            Subscription(user_id=1, channel_id=CID2),
            Subscription(user_id=1, channel_id="nebula:x"),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _settings(*, key: str = "key", units: int = 2000) -> Settings:
    return Settings(local_mode=True, youtube_api_key=key, youtube_backfill_daily_units=units)


def _item(video_id: str, days_ago: int, *, description: str = "", private: bool = False) -> dict:
    snippet = {
        "title": f"Video {video_id}",
        "description": description,
        "publishedAt": "2026-01-01T00:00:00Z",
        "resourceId": {"videoId": video_id},
    }
    if not private:
        snippet["videoOwnerChannelId"] = CID
    published = datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=days_ago)
    return {"snippet": snippet, "contentDetails": {"videoPublishedAt": published.isoformat().replace("+00:00", "Z")}}


class Api:
    """A fake playlistItems endpoint: pages per channel, keyed by page token."""

    def __init__(self, pages: dict[str, list[list[dict]]], errors: dict[str, httpx.Response] | None = None):
        self.pages = pages
        self.errors = errors or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        channel = "UC" + request.url.params["playlistId"].removeprefix("UU")
        if channel in self.errors:
            return self.errors[channel]
        pages = self.pages.get(channel, [[]])
        i = int(request.url.params.get("pageToken") or 0)
        body: dict = {"items": pages[i]}
        if i + 1 < len(pages):
            body["nextPageToken"] = str(i + 1)
        return httpx.Response(200, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_backfill_adds_older_videos_flagged_and_finishes_channels(db):
    db.add(Video(video_id="known", channel_id=CID, title="Known", published_at=datetime(2026, 9, 30, tzinfo=UTC)))
    db.commit()
    api = Api(
        {
            CID: [
                [_item("known", 1, description="Known's description"), _item("old1", 10, description="flux")],
                [_item("old2", 400), _item("gone", 500, private=True)],
            ]
        }
    )
    with api.client() as client:
        added = history.backfill_history(db, _settings(), client)
    assert added == 2

    old1 = db.get_one(Video, "old1")
    assert (old1.backfilled, old1.description, old1.channel_id) == (True, "flux", CID)
    assert old1.published_at is not None and old1.published_at.date().isoformat() == "2026-09-21"
    assert db.get(Video, "gone") is None  # private or deleted
    known = db.get_one(Video, "known")
    assert (known.backfilled, known.description) == (False, "Known's description")  # filled in, not flagged

    assert db.get_one(Channel, CID).backfilled_at is not None
    assert db.get_one(Channel, CID2).backfilled_at is not None  # empty, but done
    # Only subscribed YouTube channels; the key goes in a header, never the URL.
    playlists = {r.url.params["playlistId"] for r in api.requests}
    assert playlists == {"UU" + CID[2:], "UU" + CID2[2:]}
    assert all(r.headers["X-Goog-Api-Key"] == "key" and "key=" not in str(r.url) for r in api.requests)

    # Done channels aren't fetched again.
    with api.client() as client:
        assert history.backfill_history(db, _settings(), client) == 0
    assert len(api.requests) == 3


def test_backfill_resumes_where_it_left_off_within_the_budget(db, monkeypatch):
    monkeypatch.setattr(history, "PAGES_PER_RUN", 2)
    api = Api({CID: [[_item(f"p{i}", i)] for i in range(5)]})
    with api.client() as client:
        assert history.backfill_history(db, _settings(), client) == 2
        assert db.get_one(Channel, CID).backfill_page_token == "2"
        assert history.backfill_history(db, _settings(units=3), client) == 1
        # The day's budget is spent.
        assert history.backfill_history(db, _settings(units=3), client) == 0
    assert [r.url.params.get("pageToken") for r in api.requests] == [None, "1", "2"]


def test_quota_exhaustion_pauses_for_the_day(db):
    quota = httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    api = Api({}, errors={CID: quota})
    with api.client() as client:
        assert history.backfill_history(db, _settings(), client) == 0
        assert history.backfill_history(db, _settings(), client) == 0
    assert len(api.requests) == 1
    assert db.get_one(Channel, CID).backfilled_at is None


def test_other_failures_stop_the_run_and_retry_later(db):
    api = Api({}, errors={CID: httpx.Response(500)})
    with api.client() as client:
        history.backfill_history(db, _settings(), client)
        history.backfill_history(db, _settings(), client)
    assert len(api.requests) == 2  # tried again next run
    assert db.get_one(Channel, CID).backfilled_at is None


def test_missing_uploads_playlist_counts_as_done(db):
    api = Api({}, errors={CID: httpx.Response(404, json={"error": {"errors": [{"reason": "playlistNotFound"}]}})})
    with api.client() as client:
        history.backfill_history(db, _settings(), client)
    assert db.get_one(Channel, CID).backfilled_at is not None


@pytest.mark.parametrize(("key", "units"), [("", 2000), ("key", 0)])
def test_disabled_without_a_key_or_budget(db, key, units):
    api = Api({CID: [[_item("x", 1)]]})
    with api.client() as client:
        assert history.backfill_history(db, _settings(key=key, units=units), client) == 0
    assert api.requests == []


def test_archive_ignores_backfilled_videos(db):
    # Keep everything: without the flag, the whole back catalogue would queue.
    user = db.get_one(User, 1)
    user.download_enabled = True
    user.keep_last_n = 0
    db.add(Video(video_id="fresh", channel_id=CID, title="t", published_at=datetime(2026, 10, 1, tzinfo=UTC)))
    ancient = datetime(2019, 1, 1, tzinfo=UTC)
    db.add(Video(video_id="ancient", channel_id=CID, title="t", published_at=ancient, backfilled=True))
    db.commit()
    enqueue_pending(db)
    db.commit()
    assert set(db.execute(select(Download.video_id)).scalars()) == {"fresh"}
