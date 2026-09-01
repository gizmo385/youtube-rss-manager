"""Jellyfin playlist sync tests, driven by an in-memory fake client.

The real HTTP client can't be unit-tested without a live server; these cover the
orchestration — path-based item resolution, lazy playlist creation, and the
add/remove reconcile — which is where the logic lives.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    Category,
    CategoryPlaylist,
    ChannelCategory,
    Download,
    DownloadLink,
    JellyfinAccount,
    Video,
)
from youtube_subs_opml.web.services import jellyfin_sync
from youtube_subs_opml.web.services.crypto import encrypt_token
from youtube_subs_opml.web.services.jellyfin import PlaylistEntry, normalise_path

USER = 1
JF = "jf-guid-1"


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


class FakeJellyfin:
    """In-memory stand-in for JellyfinClient."""

    def __init__(self, episode_map: dict[str, str] | None = None):
        # file path -> item id, as Jellyfin would report once indexed
        self.episode_map = {normalise_path(p): i for p, i in (episode_map or {}).items()}
        self.playlists: dict[str, dict[str, str]] = {}  # playlist_id -> {entry_id: item_id}
        self._n = 0
        self.refreshed = 0
        self.created: list[tuple[str, str]] = []

    def refresh_library(self):
        self.refreshed += 1

    def episode_paths(self, user_id):
        return dict(self.episode_map)

    def create_playlist(self, name, user_id):
        self._n += 1
        pid = f"pl{self._n}"
        self.playlists[pid] = {}
        self.created.append((pid, name))
        return pid

    def playlist_entries(self, playlist_id, user_id):
        return [
            PlaylistEntry(item_id=i, entry_id=e)
            for e, i in self.playlists.get(playlist_id, {}).items()
        ]

    def add_to_playlist(self, playlist_id, item_ids, user_id):
        p = self.playlists.setdefault(playlist_id, {})
        for iid in item_ids:
            p[f"{playlist_id}:{iid}"] = iid

    def remove_from_playlist(self, playlist_id, entry_ids, user_id):
        p = self.playlists.get(playlist_id, {})
        for e in entry_ids:
            p.pop(e, None)

    def items_in(self, playlist_id) -> set[str]:
        return set(self.playlists.get(playlist_id, {}).values())


def seed_link(db, video_id, channel_id, link_path, item_id=None):
    db.add(Video(video_id=video_id, channel_id=channel_id, title=video_id))
    db.add(Download(video_id=video_id, status="complete", file_path="/c/" + video_id))
    db.add(DownloadLink(
        user_id=USER, video_id=video_id, link_path=link_path, jellyfin_item_id=item_id
    ))
    db.commit()


def add_category(db, cid, name, channel_ids):
    db.add(Category(id=cid, user_id=USER, name=name, slug=name.lower()))
    for ch in channel_ids:
        db.add(ChannelCategory(user_id=USER, channel_id=ch, category_id=cid))
    db.commit()


# --- item resolution -----------------------------------------------------

def test_resolve_item_ids_by_path(db):
    p1 = "/media/youtube/libraries/1/Chan/Season 2026/ep1.mkv"
    p2 = "/media/youtube/libraries/1/Chan/Season 2026/ep2.mkv"
    seed_link(db, "vid1", "chanA", p1)
    seed_link(db, "vid2", "chanA", p2)  # not yet indexed by Jellyfin

    fake = FakeJellyfin({p1: "itemA"})
    n = jellyfin_sync.resolve_item_ids(db, USER, JF, fake)

    assert n == 1
    assert db.get(DownloadLink, (USER, "vid1")).jellyfin_item_id == "itemA"
    assert db.get(DownloadLink, (USER, "vid2")).jellyfin_item_id is None


def test_resolve_tolerates_separator_differences(db):
    seed_link(db, "vid1", "chanA", "/media/youtube/libraries/1/Chan/ep1.mkv")
    # Jellyfin reports a trailing-slash / backslash variant of the same path.
    fake = FakeJellyfin({"\\media\\youtube\\libraries\\1\\Chan\\ep1.mkv": "itemA"})
    assert jellyfin_sync.resolve_item_ids(db, USER, JF, fake) == 1


# --- playlist reconcile --------------------------------------------------

def test_reconcile_creates_playlist_and_adds_items(db):
    add_category(db, 1, "Tech", ["chanA"])
    seed_link(db, "vid1", "chanA", "/p/ep1.mkv", item_id="itemA")

    fake = FakeJellyfin()
    jellyfin_sync.reconcile_playlists(db, USER, JF, fake)

    cp = db.get(CategoryPlaylist, (USER, 1))
    assert cp is not None
    assert fake.items_in(cp.playlist_id) == {"itemA"}
    assert fake.created == [(cp.playlist_id, "Tech")]


def test_reconcile_removes_stale_entries(db):
    add_category(db, 1, "Tech", ["chanA"])
    seed_link(db, "vid1", "chanA", "/p/ep1.mkv", item_id="itemA")

    # Pre-existing playlist holding a now-unwanted item alongside the wanted one.
    fake = FakeJellyfin()
    fake.playlists["plX"] = {"plX:old": "oldItem", "plX:keep": "itemA"}
    db.add(CategoryPlaylist(user_id=USER, category_id=1, playlist_id="plX"))
    db.commit()

    jellyfin_sync.reconcile_playlists(db, USER, JF, fake)

    assert fake.items_in("plX") == {"itemA"}  # stale removed, wanted kept, no dupe


def test_reconcile_is_idempotent(db):
    add_category(db, 1, "Tech", ["chanA"])
    seed_link(db, "vid1", "chanA", "/p/ep1.mkv", item_id="itemA")
    fake = FakeJellyfin()

    jellyfin_sync.reconcile_playlists(db, USER, JF, fake)
    pid = db.get(CategoryPlaylist, (USER, 1)).playlist_id
    jellyfin_sync.reconcile_playlists(db, USER, JF, fake)

    assert fake.items_in(pid) == {"itemA"}
    assert len(fake.created) == 1  # not recreated


def test_reconcile_multi_category_membership(db):
    add_category(db, 1, "Tech", ["chanA"])
    add_category(db, 2, "Fav", ["chanA"])
    seed_link(db, "vid1", "chanA", "/p/ep1.mkv", item_id="itemA")

    fake = FakeJellyfin()
    jellyfin_sync.reconcile_playlists(db, USER, JF, fake)

    p1 = db.get(CategoryPlaylist, (USER, 1)).playlist_id
    p2 = db.get(CategoryPlaylist, (USER, 2)).playlist_id
    assert fake.items_in(p1) == {"itemA"}
    assert fake.items_in(p2) == {"itemA"}


def test_reconcile_removal_failure_is_nonfatal(db):
    """An older Jellyfin that 400s on API-key delete must not break the sync."""
    from youtube_subs_opml.web.services.jellyfin import JellyfinError

    add_category(db, 1, "Tech", ["chanA"])
    seed_link(db, "vid1", "chanA", "/p/ep1.mkv", item_id="itemA")
    fake = FakeJellyfin()
    fake.playlists["plX"] = {"plX:old": "oldItem"}
    db.add(CategoryPlaylist(user_id=USER, category_id=1, playlist_id="plX"))
    db.commit()

    def boom(*a, **k):
        raise JellyfinError("400")
    fake.remove_from_playlist = boom

    # Should not raise; the wanted item is still added.
    jellyfin_sync.reconcile_playlists(db, USER, JF, fake)
    assert "itemA" in fake.items_in("plX")


# --- sync_user + sync_all -----------------------------------------------

def test_sync_user_refreshes_only_when_unresolved(db):
    add_category(db, 1, "Tech", ["chanA"])
    seed_link(db, "vid1", "chanA", "/p/ep1.mkv")  # unresolved
    fake = FakeJellyfin({"/p/ep1.mkv": "itemA"})

    jellyfin_sync.sync_user(db, USER, JF, fake)
    assert fake.refreshed == 1                       # refreshed because unresolved
    assert db.get(DownloadLink, (USER, "vid1")).jellyfin_item_id == "itemA"
    assert fake.items_in(db.get(CategoryPlaylist, (USER, 1)).playlist_id) == {"itemA"}

    # Second pass: nothing unresolved -> no refresh.
    fake.refreshed = 0
    jellyfin_sync.sync_user(db, USER, JF, fake)
    assert fake.refreshed == 0


def test_sync_all_skips_account_without_user_guid(db):
    db.add(JellyfinAccount(
        user_id=USER, base_url="https://jf", api_key_encrypted=encrypt_token("k"),
        jellyfin_user_id="",
    ))
    db.commit()
    # No network call should happen (client never built) and nothing errors.
    jellyfin_sync.sync_all(db)
    assert db.query(CategoryPlaylist).count() == 0
