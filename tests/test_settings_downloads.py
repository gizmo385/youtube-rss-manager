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

from datetime import datetime, timedelta, timezone

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
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
    assert 'data-tab="downloads"' in resp.text  # the Downloads tab
    assert "Download history" in resp.text       # the panel heading
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


# --- service: full history list (filters + pagination) -------------------

@pytest.fixture
def history_db():
    """A fresh session with 60 downloads across two channels and one category."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(User(id=1, oidc_sub="s", email="e"))
        s.add(Channel(channel_id="UCa", title="Alpha"))
        s.add(Channel(channel_id="UCb", title="Beta"))
        s.add(Subscription(user_id=1, channel_id="UCa"))
        s.add(Subscription(user_id=1, channel_id="UCb"))
        s.add(Category(id=1, user_id=1, name="Tech", slug="tech"))
        s.add(ChannelCategory(user_id=1, channel_id="UCa", category_id=1))
        s.commit()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        statuses = ["complete", "failed", "skipped", "pending"]
        for i in range(60):
            cid = "UCa" if i % 2 else "UCb"
            st = statuses[i % 4]
            s.add(Video(video_id=f"v{i:09d}", channel_id=cid, title=f"Vid {i}",
                        published_at=base + timedelta(days=i)))
            s.add(Download(video_id=f"v{i:09d}", status=st,
                           skip_reason="unavailable" if st == "skipped" else None))
        s.commit()
    return Session


def test_list_paginates(history_db):
    with history_db() as db:
        p1 = downloads.list_downloads(db, 1, page=1)
        assert p1["total"] == 60 and p1["pages"] == 3
        assert len(p1["rows"]) == 25
        assert (p1["start"], p1["end"]) == (1, 25)
        assert p1["has_prev"] is False and p1["has_next"] is True
        p3 = downloads.list_downloads(db, 1, page=3)
        assert len(p3["rows"]) == 10 and p3["end"] == 60 and p3["has_next"] is False
        # Out-of-range page clamps rather than returning an empty page.
        assert downloads.list_downloads(db, 1, page=99)["page"] == 3


def test_list_orders_newest_first(history_db):
    with history_db() as db:
        rows = downloads.list_downloads(db, 1, page=1)["rows"]
    # Video 59 is the most recent (largest published_at).
    assert rows[0]["video_id"] == "v000000059"


def test_list_filters(history_db):
    with history_db() as db:
        assert downloads.list_downloads(db, 1, channel_id="UCa")["total"] == 30
        assert downloads.list_downloads(db, 1, category_id=1)["total"] == 30
        assert downloads.list_downloads(db, 1, status="failed")["total"] == 15
        # Title search: "Vid 1" matches 1 and 10–19 == 11 rows.
        assert downloads.list_downloads(db, 1, q="Vid 1")["total"] == 11
        # Combined filters intersect: all 15 failures happen to sit on UCa
        # (odd indices), so UCa+failed keeps them and UCb+failed drops them all.
        assert downloads.list_downloads(db, 1, channel_id="UCa", status="failed")["total"] == 15
        assert downloads.list_downloads(db, 1, channel_id="UCb", status="failed")["total"] == 0


def test_subscribed_channels_sorted(history_db):
    with history_db() as db:
        chans = downloads.subscribed_channels(db, 1)
    assert [c["title"] for c in chans] == ["Alpha", "Beta"]


# --- friendly display labels ---------------------------------------------

def test_table_uses_friendly_labels(app_db):
    client, _Session = app_db
    html = client.get("/settings/downloads/table").text
    # Statuses and skip reasons render as human labels, not raw values.
    for label in ("Archived", "Failed", "Unavailable", "Too long", "Short", "Queued"):
        assert label in html
    assert "too_long" not in html
    assert "members_only" not in html


def test_no_subscribers_reads_as_not_wanted(app_db):
    client, Session = app_db
    with Session() as db:
        db.add(Video(video_id="vnowant00001", channel_id=SUB, title="Nobody wants me"))
        db.add(Download(video_id="vnowant00001", status="skipped",
                        skip_reason="no_subscribers"))
        db.commit()
    html = client.get("/settings/downloads/table", params={"status": "skipped"}).text
    assert "Not wanted" in html
    assert "no_subscribers" not in html


# --- routes: tabs, table partial, htmx retry -----------------------------

def test_settings_has_two_tabs(app_db):
    client, _Session = app_db
    html = client.get("/settings").text
    assert 'id="panel-settings"' in html and 'id="panel-downloads"' in html
    assert 'data-tab="downloads"' in html
    # Settings tab is the default; the downloads panel starts hidden.
    assert 'id="panel-downloads" class="tab-panel" hidden' in html


def test_tab_query_param_opens_downloads(app_db):
    client, _Session = app_db
    html = client.get("/settings?tab=downloads").text
    assert 'id="panel-settings" class="tab-panel" hidden' in html


def test_table_partial_filters_by_status(app_db):
    client, _Session = app_db
    # SUB has 3 skipped rows (unavailable, too_long, short) in full history.
    resp = client.get("/settings/downloads/table", params={"status": "skipped"})
    assert resp.status_code == 200
    assert "of 3" in resp.text
    assert "Title vshort000001" in resp.text  # full history includes Shorts
    # A status filter with no matches renders the empty state, not an error.
    empty = client.get("/settings/downloads/table", params={"status": "downloading"})
    assert "No downloads match these filters" in empty.text


def test_htmx_retry_returns_refresh_partial(app_db):
    client, Session = app_db
    resp = client.post(
        "/settings/downloads/vfailed00001/retry",
        headers={"HX-Request": "true"},
        data={"status": "", "page": "1"},
    )
    assert resp.status_code == 200
    # Table body + out-of-band summary come back together.
    assert 'id="dl-page"' in resp.text
    assert 'id="dl-summary" class="dl-summary" hx-swap-oob="true"' in resp.text
    # Recoverable dropped from 2 to 1 after requeuing the failure.
    assert "Retry all recoverable (1)" in resp.text
    with Session() as db:
        assert db.get(Download, "vfailed00001").status == "pending"


def test_htmx_retry_all_returns_refresh_partial(app_db):
    client, Session = app_db
    resp = client.post(
        "/settings/downloads/retry-all",
        headers={"HX-Request": "true"},
        data={"page": "1"},
    )
    assert resp.status_code == 200
    assert 'hx-swap-oob="true"' in resp.text
    # Nothing recoverable left, so the button is gone.
    assert "Retry all recoverable" not in resp.text
    with Session() as db:
        assert db.get(Download, "vfailed00001").status == "pending"
        assert db.get(Download, "vunavail0001").status == "pending"
