"""Downloader panel: status scoping, problem list, and manual retry.

In-memory SQLite with get_db / get_current_user overridden — no Postgres,
Keycloak, or network. Exercises both the service layer and the settings routes.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import (
    Channel,
    Download,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.routes import settings
from youtube_subs_opml.web.services import downloads

USER_ID = 1
OTHER_ID = 2
SUB = "UCsubscribed000000000"
IGN = "UCignored00000000000"
OTHER = "UCotherusers00000000"


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _dl(seed, channel_id, vid, status, *, skip_reason=None, attempts=1,
        last_error=None, size=None):
    seed.add(Video(video_id=vid, channel_id=channel_id, title=f"Title {vid}"))
    seed.add(Download(video_id=vid, status=status, skip_reason=skip_reason,
                      attempts=attempts, last_error=last_error,
                      file_size_bytes=size,
                      file_path="/c/%s.mkv" % vid if status == "complete" else None))


@pytest.fixture
def app_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    with TestingSession() as seed:
        seed.add(User(id=USER_ID, oidc_sub="s1", email="e1"))
        seed.add(User(id=OTHER_ID, oidc_sub="s2", email="e2"))
        for cid in (SUB, IGN, OTHER):
            seed.add(Channel(channel_id=cid, title=f"Chan {cid[-4:]}"))
        seed.commit()
        seed.add(Subscription(user_id=USER_ID, channel_id=SUB, ignored=False))
        seed.add(Subscription(user_id=USER_ID, channel_id=IGN, ignored=True))
        seed.add(Subscription(user_id=OTHER_ID, channel_id=OTHER, ignored=False))

        # Subscribed channel: a mix of states.
        _dl(seed, SUB, "vcomplete001", "complete", size=1000)
        _dl(seed, SUB, "vfailed00001", "failed", attempts=5, last_error="boom")
        _dl(seed, SUB, "vunavail0001", "skipped", skip_reason="unavailable",
            attempts=5, last_error="Sign in to confirm you're not a bot")
        _dl(seed, SUB, "vtoolong0001", "skipped", skip_reason="too_long")
        _dl(seed, SUB, "vshort000001", "skipped", skip_reason="short")
        _dl(seed, SUB, "vpending0001", "pending")
        # Ignored subscription and another user's channel: never visible here.
        _dl(seed, IGN, "vignored0001", "failed", last_error="hidden")
        _dl(seed, OTHER, "vother000001", "failed", last_error="not mine")
        seed.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def override_user(db: Session = Depends(get_db)):
        return db.get(User, USER_ID)

    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    return TestClient(app), TestingSession


# --- service: scoping & listing -----------------------------------------

def test_status_counts_scoped_to_subscriptions(app_db):
    _client, Session = app_db
    with Session() as db:
        counts = downloads.status_counts(db, USER_ID)
    # complete + failed + pending + 3 skipped (unavailable/too_long/short).
    assert counts == {"complete": 1, "failed": 1, "pending": 1, "skipped": 3}
    # Ignored sub and the other user's channel contribute nothing.


def test_problem_list_excludes_muted_and_unscoped(app_db):
    _client, Session = app_db
    with Session() as db:
        problems = downloads.problem_downloads(db, USER_ID)
    ids = {p["video_id"] for p in problems}
    assert ids == {"vfailed00001", "vunavail0001", "vtoolong0001"}
    # short is muted; complete/pending aren't problems; ignored & other-user hidden.
    assert "vshort000001" not in ids
    assert "vignored0001" not in ids
    assert "vother000001" not in ids


def test_problem_row_carries_error_and_attempts(app_db):
    _client, Session = app_db
    with Session() as db:
        problems = downloads.problem_downloads(db, USER_ID)
    row = next(p for p in problems if p["video_id"] == "vunavail0001")
    assert row["status"] == "skipped"
    assert row["skip_reason"] == "unavailable"
    assert row["attempts"] == 5
    assert "bot" in row["last_error"]


# --- service: retry ------------------------------------------------------

def test_retry_one_requeues_and_clears(app_db):
    _client, Session = app_db
    with Session() as db:
        assert downloads.retry_one(db, USER_ID, "vfailed00001") is True
        row = db.get(Download, "vfailed00001")
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.skip_reason is None
        assert row.next_attempt_at is None
        assert row.last_error is None


def test_retry_one_rejects_complete(app_db):
    _client, Session = app_db
    with Session() as db:
        assert downloads.retry_one(db, USER_ID, "vcomplete001") is False
        assert db.get(Download, "vcomplete001").status == "complete"


def test_retry_one_rejects_unscoped_and_ignored(app_db):
    _client, Session = app_db
    with Session() as db:
        # Another user's channel and an ignored subscription are both invisible.
        assert downloads.retry_one(db, USER_ID, "vother000001") is False
        assert downloads.retry_one(db, USER_ID, "vignored0001") is False
        assert db.get(Download, "vother000001").status == "failed"
        assert db.get(Download, "vignored0001").status == "failed"


def test_retry_all_recoverable_targets_failed_and_unavailable(app_db):
    _client, Session = app_db
    with Session() as db:
        n = downloads.retry_all_recoverable(db, USER_ID)
        assert n == 2  # vfailed + vunavail
        assert db.get(Download, "vfailed00001").status == "pending"
        assert db.get(Download, "vunavail0001").status == "pending"
        # Deliberate skips are left alone.
        assert db.get(Download, "vtoolong0001").status == "skipped"
        assert db.get(Download, "vshort000001").status == "skipped"


# --- routes --------------------------------------------------------------

def test_settings_page_renders_downloader_panel(app_db):
    client, _Session = app_db
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Downloader" in resp.text
    assert "Title vfailed00001" in resp.text
    assert "Retry all recoverable (2)" in resp.text


def test_retry_route_requeues(app_db):
    client, Session = app_db
    resp = client.post("/settings/downloads/vfailed00001/retry", follow_redirects=False)
    assert resp.status_code == 303
    assert "dl=retried" in resp.headers["location"]
    with Session() as db:
        assert db.get(Download, "vfailed00001").status == "pending"


def test_retry_route_rejects_unscoped(app_db):
    client, Session = app_db
    resp = client.post("/settings/downloads/vother000001/retry", follow_redirects=False)
    assert resp.status_code == 303
    assert "dl=notfound" in resp.headers["location"]
    with Session() as db:
        assert db.get(Download, "vother000001").status == "failed"


def test_retry_all_route(app_db):
    client, Session = app_db
    resp = client.post("/settings/downloads/retry-all", follow_redirects=False)
    assert resp.status_code == 303
    assert "dl=retried&dl_n=2" in resp.headers["location"]
    with Session() as db:
        assert db.get(Download, "vunavail0001").status == "pending"
