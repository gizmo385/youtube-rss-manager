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
    FreshRssAccount,
    FreshRssItem,
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
            "keep_last_n_audio": "30",
            "max_duration_minutes": "90",
            "min_duration_minutes": "3",
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
        assert user.keep_last_n_audio == 30
        assert user.max_duration_seconds == 5400  # 90 minutes
        assert user.min_duration_seconds == 180  # 3 minutes
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
        assert user.keep_last_n == 15  # blank → default
        assert user.keep_last_n_audio is None  # blank → same window as video
        assert user.max_duration_seconds == 0
        assert user.min_duration_seconds == 0  # blank → no floor
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
        data={"base_url": "https://new.example.com", "api_key": "", "jellyfin_user_id": "guid-2"},
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


def test_jellyfin_sync_requires_account(app_db):
    client, _ = app_db
    resp = client.post("/settings/jellyfin/sync", follow_redirects=False)
    assert resp.headers["location"] == "/settings?jellyfin_sync=missing"


def test_jellyfin_sync_requires_user_guid(app_db):
    client, _ = app_db
    client.post(
        "/settings/jellyfin",
        data={"base_url": "https://jf.example.com", "api_key": "k"},  # no GUID
        follow_redirects=False,
    )
    resp = client.post("/settings/jellyfin/sync", follow_redirects=False)
    assert resp.headers["location"] == "/settings?jellyfin_sync=no_guid"


def test_jellyfin_sync_runs_for_this_user(app_db, monkeypatch):
    client, _ = app_db
    client.post(
        "/settings/jellyfin",
        data={"base_url": "https://jf.example.com", "api_key": "k", "jellyfin_user_id": "guid-1"},
        follow_redirects=False,
    )

    from youtube_subs_opml.web.services import jellyfin_sync

    seen = {}
    monkeypatch.setattr(
        jellyfin_sync,
        "sync_user",
        lambda db, user_id, jf_user_id, client: seen.update(user_id=user_id, guid=jf_user_id),
    )
    resp = client.post("/settings/jellyfin/sync", follow_redirects=False)
    assert resp.headers["location"] == "/settings?jellyfin_sync=ok"
    assert seen == {"user_id": USER_ID, "guid": "guid-1"}


def test_freshrss_account_upsert_keeps_password_when_blank(app_db):
    client, Session_ = app_db
    form = {"base_url": "https://rss.example.com", "username": "me@example.com", "api_password": "pw-1"}
    client.post("/settings/freshrss", data=form, follow_redirects=False)
    with Session_() as db:
        acct = db.query(FreshRssAccount).one()
        assert (acct.base_url, acct.username) == ("https://rss.example.com", "me@example.com")
        assert decrypt_token(acct.api_password_encrypted) == "pw-1"

    client.post("/settings/freshrss", data={**form, "username": "other", "api_password": ""}, follow_redirects=False)
    with Session_() as db:
        acct = db.query(FreshRssAccount).one()
        assert acct.username == "other"
        assert decrypt_token(acct.api_password_encrypted) == "pw-1"


def test_freshrss_create_requires_password(app_db):
    client, _ = app_db
    resp = client.post(
        "/settings/freshrss",
        data={"base_url": "https://rss.example.com", "username": "me", "api_password": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_freshrss_test_and_sync(app_db, monkeypatch):
    client, Session_ = app_db
    assert client.post("/settings/freshrss/sync", follow_redirects=False).headers["location"] == (
        "/settings?freshrss_sync=missing"
    )
    client.post(
        "/settings/freshrss",
        data={"base_url": "https://rss.example.com", "username": "me", "api_password": "pw"},
        follow_redirects=False,
    )

    from youtube_subs_opml.web.services.freshrss import FreshRssClient

    monkeypatch.setattr(FreshRssClient, "verify", lambda self: True)
    assert client.post("/settings/freshrss/test", follow_redirects=False).headers["location"] == (
        "/settings?freshrss_test=ok"
    )
    with Session_() as db:
        assert db.query(FreshRssAccount).one().last_verified_at is not None

    from youtube_subs_opml.web.routes import settings as settings_routes

    seen = []
    monkeypatch.setattr(settings_routes, "sync_one", lambda db, user_id: seen.append(user_id))
    assert client.post("/settings/freshrss/sync", follow_redirects=False).headers["location"] == (
        "/settings?freshrss_sync=ok"
    )
    assert seen == [USER_ID]


def test_freshrss_disconnect_forgets_items(app_db):
    client, Session_ = app_db
    client.post(
        "/settings/freshrss",
        data={"base_url": "https://rss.example.com", "username": "me", "api_password": "pw"},
        follow_redirects=False,
    )
    with Session_() as db:
        db.add(FreshRssItem(user_id=USER_ID, item_id=1, video_key="v", synced_read=True))
        db.commit()
    client.post("/settings/freshrss/delete", follow_redirects=False)
    with Session_() as db:
        assert db.query(FreshRssAccount).count() == 0
        assert db.query(FreshRssItem).count() == 0


def test_settings_page_renders_freshrss_card(app_db):
    client, _ = app_db
    resp = client.get("/settings")
    assert 'action="/settings/freshrss"' in resp.text
    assert 'name="username"' in resp.text and 'value="e"' in resp.text  # prefilled from the user's email


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

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "download_enabled", "value": "true"})
    assert sub().download_enabled is True

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "download_enabled", "value": "inherit"})
    assert sub().download_enabled is None

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "keep_last_n", "value": "0"})
    assert sub().keep_last_n == 0

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "max_duration_seconds", "value": "90"})
    assert sub().max_duration_seconds == 5400  # minutes → seconds

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "min_duration_seconds", "value": "2"})
    assert sub().min_duration_seconds == 120  # minutes → seconds

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "keep_last_n_audio", "value": "30"})
    assert sub().keep_last_n_audio == 30

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "link_target", "value": "when_ready"})
    assert sub().link_target == "when_ready"


def test_channel_archive_pref_rejects_unknown_field(app_db):
    client, _ = app_db
    resp = client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "evil", "value": "x"})
    assert resp.status_code == 400


# --- per-category archive prefs -----------------------------------------


def test_category_archive_pref_persists(app_db):
    client, Session_ = app_db
    resp = client.patch("/categories/1/archive-pref", data={"field": "download_enabled", "value": "true"})
    assert resp.status_code == 200
    with Session_() as db:
        assert db.get(Category, 1).download_enabled is True

    client.patch("/categories/1/archive-pref", data={"field": "max_duration_seconds", "value": "30"})
    with Session_() as db:
        assert db.get(Category, 1).max_duration_seconds == 1800

    client.patch("/categories/1/archive-pref", data={"field": "min_duration_seconds", "value": "4"})
    with Session_() as db:
        assert db.get(Category, 1).min_duration_seconds == 240


# --- disk usage + failure surfacing -------------------------------------


def test_disk_usage_and_failure_counts_render(app_db):
    client, Session_ = app_db
    with Session_() as db:
        db.add(ChannelCategory(user_id=USER_ID, channel_id=CID, category_id=1))
        db.add(Video(video_id="vidaaaaaaaa", channel_id=CID, title="a"))
        db.add(Video(video_id="vidbbbbbbbb", channel_id=CID, title="b"))
        db.commit()
        db.add(Download(video_id="vidaaaaaaaa", status="complete", file_size_bytes=5 * 1024 * 1024))
        db.add(Download(video_id="vidbbbbbbbb", status="failed"))
        db.commit()

    resp = client.get("/channels")
    assert resp.status_code == 200
    assert "5.0 MB" in resp.text  # per-category disk usage
    assert "1 download failed" in resp.text  # failure surfacing


# --- SponsorBlock -----------------------------------------------------------


def test_user_sponsorblock_defaults_persist(app_db):
    client, Session_ = app_db
    client.post(
        "/settings/defaults",
        data={"sponsorblock": "skip", "sponsorblock_categories": ["outro", "sponsor", "bogus"]},
        follow_redirects=False,
    )
    with Session_() as db:
        user = db.get(User, USER_ID)
        assert user.sponsorblock == "skip"
        assert user.sponsorblock_categories == "sponsor,outro"  # known kinds, canonical order

    # A bad mode falls back to off; no kinds ticked is allowed.
    client.post("/settings/defaults", data={"sponsorblock": "bogus"}, follow_redirects=False)
    with Session_() as db:
        user = db.get(User, USER_ID)
        assert user.sponsorblock == "off"
        assert user.sponsorblock_categories == ""


def test_user_podcast_sponsorblock_persists_separately(app_db):
    client, Session_ = app_db
    client.post(
        "/settings/defaults",
        data={
            "sponsorblock": "show",
            "sponsorblock_categories": ["sponsor"],
            "podcast_sponsorblock": "remove",
            "podcast_sponsorblock_categories": ["intro", "sponsor"],
        },
        follow_redirects=False,
    )
    with Session_() as db:
        user = db.get_one(User, USER_ID)
        assert (user.sponsorblock, user.sponsorblock_categories) == ("show", "sponsor")
        assert (user.podcast_sponsorblock, user.podcast_sponsorblock_categories) == ("remove", "sponsor,intro")

    client.post("/settings/defaults", data={"podcast_sponsorblock": "skip"}, follow_redirects=False)
    with Session_() as db:
        assert db.get_one(User, USER_ID).podcast_sponsorblock == "off"  # not a podcast mode


def test_settings_page_renders_sponsorblock(app_db):
    client, _ = app_db
    html = client.get("/settings").text
    assert '<select name="sponsorblock">' in html
    assert 'name="sponsorblock_categories" value="sponsor" checked' in html
    assert 'name="sponsorblock_categories" value="outro">' in html
    assert '<select name="podcast_sponsorblock">' in html
    assert 'name="podcast_sponsorblock_categories" value="sponsor" checked' in html


def test_sponsorblock_pref_on_channels_and_categories(app_db):
    client, Session_ = app_db
    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "sponsorblock", "value": "show"})
    client.patch("/categories/1/archive-pref", data={"field": "sponsorblock", "value": "skip"})
    with Session_() as db:
        assert db.get(Subscription, (USER_ID, CID)).sponsorblock == "show"
        assert db.get(Category, 1).sponsorblock == "skip"

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "podcast_sponsorblock", "value": "remove"})
    client.patch("/categories/1/archive-pref", data={"field": "podcast_sponsorblock", "value": "off"})
    with Session_() as db:
        assert db.get(Subscription, (USER_ID, CID)).podcast_sponsorblock == "remove"
        assert db.get(Category, 1).podcast_sponsorblock == "off"

    client.post("/channels/archive-pref", data={"channel_ids": CID, "field": "sponsorblock", "value": "inherit"})
    client.patch("/categories/1/archive-pref", data={"field": "sponsorblock", "value": "nonsense"})
    with Session_() as db:
        assert db.get(Subscription, (USER_ID, CID)).sponsorblock is None
        assert db.get(Category, 1).sponsorblock is None
