"""Phase 2 settings-UI tests: archive defaults, Jellyfin account, and the
per-channel / per-category archive preference controls.

Runs against in-memory SQLite with get_db / get_current_user overridden, so no
Postgres, Keycloak, or network is needed.
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
    Category,
    Channel,
    ChannelCategory,
    Download,
    JellyfinAccount,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.routes import categories, channels, settings
from youtube_subs_opml.web.services.crypto import decrypt_token
from youtube_subs_opml.web.services.jellyfin import JellyfinClient

USER_ID = 1
CID = "UCchannel0000000000000"


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch):
    """Run these tests under LOCAL_MODE so get_settings() has usable defaults
    (fernet key, media_root, base_url) without a real .env DATABASE_URL."""
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def app_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    with TestingSession() as seed:
        seed.add(User(id=USER_ID, oidc_sub="s", email="e"))
        seed.add(Channel(channel_id=CID, title="Chan"))
        seed.commit()
        seed.add(Subscription(user_id=USER_ID, channel_id=CID))
        seed.add(Category(id=1, user_id=USER_ID, name="Tech", slug="tech"))
        seed.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    # FastAPI caches get_db within a request, so get_current_user and the route
    # share the same session — mutations on the user object persist.
    def override_user(db: Session = Depends(get_db)):
        return db.get(User, USER_ID)

    app = FastAPI()
    app.include_router(settings.router)
    app.include_router(channels.router)
    app.include_router(categories.router)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    return TestClient(app), TestingSession


# --- user-level archive defaults ----------------------------------------

def test_user_archive_defaults_persist(app_db):
    client, Session_ = app_db
    resp = client.post(
        "/settings/defaults",
        data={
            "download_enabled": "true",
            "generate_podcast": "true",
            "keep_last_n": "5",
            "max_duration_minutes": "90",
            "link_target": "hold",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with Session_() as db:
        user = db.get(User, USER_ID)
        assert user.download_enabled is True
        assert user.generate_podcast is True
        assert user.keep_last_n == 5
        assert user.max_duration_seconds == 5400  # 90 minutes
        assert user.link_target == "hold"


def test_user_defaults_unchecked_and_bad_link_target(app_db):
    client, Session_ = app_db
    # Omitting checkboxes = off; an unknown link_target falls back to youtube.
    client.post(
        "/settings/defaults",
        data={"keep_last_n": "", "max_duration_minutes": "", "link_target": "bogus"},
        follow_redirects=False,
    )
    with Session_() as db:
        user = db.get(User, USER_ID)
        assert user.download_enabled is False
        assert user.generate_podcast is False
        assert user.keep_last_n == 15   # blank → default
        assert user.max_duration_seconds == 0
        assert user.link_target == "youtube"


# --- Jellyfin account ----------------------------------------------------

def test_jellyfin_account_upsert_keeps_key_when_blank(app_db):
    client, Session_ = app_db
    client.post(
        "/settings/jellyfin",
        data={
            "base_url": "https://jf.example.com",
            "api_key": "secret-key-123",
            "jellyfin_user_id": "guid-1",
        },
        follow_redirects=False,
    )
    with Session_() as db:
        acct = db.query(JellyfinAccount).one()
        assert acct.base_url == "https://jf.example.com"
        assert decrypt_token(acct.api_key_encrypted) == "secret-key-123"

    # Update base_url with a blank key — key must be preserved.
    client.post(
        "/settings/jellyfin",
        data={"base_url": "https://new.example.com", "api_key": "",
              "jellyfin_user_id": "guid-2"},
        follow_redirects=False,
    )
    with Session_() as db:
        acct = db.query(JellyfinAccount).one()
        assert acct.base_url == "https://new.example.com"
        assert acct.jellyfin_user_id == "guid-2"
        assert decrypt_token(acct.api_key_encrypted) == "secret-key-123"


def test_jellyfin_create_requires_key(app_db):
    client, _ = app_db
    resp = client.post(
        "/settings/jellyfin",
        data={"base_url": "https://jf.example.com", "api_key": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_jellyfin_test_ok_and_fail(app_db, monkeypatch):
    client, Session_ = app_db
    client.post(
        "/settings/jellyfin",
        data={"base_url": "https://jf.example.com", "api_key": "k"},
        follow_redirects=False,
    )

    monkeypatch.setattr(JellyfinClient, "verify", lambda self: True)
    resp = client.post("/settings/jellyfin/test", follow_redirects=False)
    assert resp.headers["location"] == "/settings?jellyfin_test=ok"
    with Session_() as db:
        assert db.query(JellyfinAccount).one().last_verified_at is not None

    monkeypatch.setattr(JellyfinClient, "verify", lambda self: False)
    resp = client.post("/settings/jellyfin/test", follow_redirects=False)
    assert resp.headers["location"] == "/settings?jellyfin_test=fail"


def test_settings_page_renders_library_path(app_db):
    client, _ = app_db
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert f"/libraries/{USER_ID}" in resp.text  # library folder shown


# --- per-channel archive prefs ------------------------------------------

def test_channel_archive_pref_tristate_and_ints(app_db):
    client, Session_ = app_db

    def sub():
        with Session_() as db:
            return db.get(Subscription, (USER_ID, CID))

    client.post("/channels/archive-pref",
                data={"channel_ids": CID, "field": "download_enabled", "value": "true"})
    assert sub().download_enabled is True

    client.post("/channels/archive-pref",
                data={"channel_ids": CID, "field": "download_enabled", "value": "inherit"})
    assert sub().download_enabled is None

    client.post("/channels/archive-pref",
                data={"channel_ids": CID, "field": "keep_last_n", "value": "0"})
    assert sub().keep_last_n == 0

    client.post("/channels/archive-pref",
                data={"channel_ids": CID, "field": "max_duration_seconds", "value": "90"})
    assert sub().max_duration_seconds == 5400  # minutes → seconds

    client.post("/channels/archive-pref",
                data={"channel_ids": CID, "field": "link_target", "value": "when_ready"})
    assert sub().link_target == "when_ready"


def test_channel_archive_pref_rejects_unknown_field(app_db):
    client, _ = app_db
    resp = client.post("/channels/archive-pref",
                       data={"channel_ids": CID, "field": "evil", "value": "x"})
    assert resp.status_code == 400


# --- per-category archive prefs -----------------------------------------

def test_category_archive_pref_persists(app_db):
    client, Session_ = app_db
    resp = client.patch("/categories/1/archive-pref",
                        data={"field": "download_enabled", "value": "true"})
    assert resp.status_code == 200
    with Session_() as db:
        assert db.get(Category, 1).download_enabled is True

    client.patch("/categories/1/archive-pref",
                 data={"field": "max_duration_seconds", "value": "30"})
    with Session_() as db:
        assert db.get(Category, 1).max_duration_seconds == 1800


# --- disk usage + failure surfacing -------------------------------------

def test_disk_usage_and_failure_counts_render(app_db):
    client, Session_ = app_db
    with Session_() as db:
        db.add(ChannelCategory(user_id=USER_ID, channel_id=CID, category_id=1))
        db.add(Video(video_id="vidaaaaaaaa", channel_id=CID, title="a"))
        db.add(Video(video_id="vidbbbbbbbb", channel_id=CID, title="b"))
        db.commit()
        db.add(Download(video_id="vidaaaaaaaa", status="complete",
                        file_size_bytes=5 * 1024 * 1024))
        db.add(Download(video_id="vidbbbbbbbb", status="failed"))
        db.commit()

    resp = client.get("/channels")
    assert resp.status_code == 200
    assert "5.0 MB" in resp.text          # per-category disk usage
    assert "1 download failed" in resp.text  # failure surfacing
