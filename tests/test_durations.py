"""Video lengths from the Data API: batched, within a budget, newest feed
videos first, never overwriting the downloader's probe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.config import Settings
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import Channel, Video
from youtube_subs_opml.web.services import durations

CID = "UCaaaaaaaaaaaaaaaaaaaaaa"
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_state():
    for s in (durations._spent, durations._exhausted, durations._missing):
        s.clear()
    yield
    for s in (durations._spent, durations._exhausted, durations._missing):
        s.clear()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Channel(channel_id=CID, title="A"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _settings(*, key: str = "key", units: int = 500) -> Settings:
    return Settings(local_mode=True, youtube_api_key=key, youtube_duration_daily_units=units)


def _video(db, vid: str, hours_ago: int, *, duration: int | None = None, backfilled: bool = False) -> None:
    db.add(
        Video(
            video_id=vid,
            channel_id=CID,
            title=vid,
            published_at=NOW - timedelta(hours=hours_ago),
            duration_seconds=duration,
            backfilled=backfilled,
        )
    )
    db.commit()


class Api:
    """A fake videos endpoint: ``lengths`` maps id -> ISO duration; ids not in
    it are left out of the response, like a deleted video."""

    def __init__(self, lengths: dict[str, str], status: int = 200, reason: str = "") -> None:
        self.lengths, self.status, self.reason = lengths, status, reason
        self.calls: list[list[str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Goog-Api-Key"] == "key"
        assert request.url.params["part"] == "contentDetails"
        ids = request.url.params["id"].split(",")
        self.calls.append(ids)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"errors": [{"reason": self.reason}]}})
        items = [{"id": i, "contentDetails": {"duration": self.lengths[i]}} for i in ids if i in self.lengths]
        return httpx.Response(200, json={"items": items})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _lengths(db) -> dict[str, int | None]:
    return dict(db.execute(select(Video.video_id, Video.duration_seconds)).all())


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT1H2M3S", 3723), ("PT7M16S", 436), ("PT45S", 45), ("P1DT1S", 86401), ("P0D", None), ("", None), ("x", None)],
)
def test_parse_duration(value, seconds):
    assert durations.parse_duration(value) == seconds


def test_fills_missing_lengths_and_leaves_probed_ones(db):
    _video(db, "a", 1)
    _video(db, "b", 2, duration=999)
    api = Api({"a": "PT7M16S", "b": "PT1M"})
    assert durations.fill_durations(db, _settings(), api.client()) == 1
    assert api.calls == [["a"]]
    assert _lengths(db) == {"a": 436, "b": 999}


def test_batches_of_fifty_feed_videos_first_newest_first(db):
    for i in range(60):
        _video(db, f"v{i:02}", i)
    _video(db, "old", 1, backfilled=True)
    api = Api({})
    durations.fill_durations(db, _settings(), api.client())
    assert [len(c) for c in api.calls] == [50, 11]
    assert api.calls[0][0] == "v00"
    assert api.calls[1][-1] == "old"


def test_ids_the_api_leaves_out_are_not_asked_about_again(db):
    _video(db, "gone", 1)
    _video(db, "live", 2)
    api = Api({"live": "P0D"})
    durations.fill_durations(db, _settings(), api.client())
    durations.fill_durations(db, _settings(), api.client())
    assert api.calls == [["gone", "live"], ["live"]]
    assert _lengths(db) == {"gone": None, "live": None}


def test_daily_budget_caps_calls(db):
    for i in range(200):
        _video(db, f"v{i:03}", i)
    api = Api({})
    durations.fill_durations(db, _settings(units=2), api.client())
    durations.fill_durations(db, _settings(units=2), api.client())
    assert len(api.calls) == 2


def test_quota_exhausted_pauses_for_the_day(db):
    _video(db, "a", 1)
    api = Api({}, status=403, reason="quotaExceeded")
    assert durations.fill_durations(db, _settings(), api.client()) == 0
    durations.fill_durations(db, _settings(), api.client())
    assert len(api.calls) == 1


def test_no_key_does_nothing(db):
    _video(db, "a", 1)
    api = Api({"a": "PT1M"})
    assert durations.fill_durations(db, _settings(key=""), api.client()) == 0
    assert api.calls == []
