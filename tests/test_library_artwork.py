"""Jellyfin series/season artwork: channel-art backfill and library sync.

In-memory SQLite with a real temp media tree so the poster hardlinks and inode
sharing are exercised for real. Network (yt-dlp probe, HTTP image fetch) is
stubbed — the point here is the on-disk layout and teardown, not YouTube.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.downloader import naming, worker, ytdlp
from youtube_subs_opml.downloader.worker import (
    backfill_channel_art,
    reconcile_links,
    run_prune,
    sync_library_art,
)
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    Channel,
    Download,
    Subscription,
    User,
    Video,
)

CHAN = "UCchannel0000000000000"
CHAN_TITLE = "Test Chan"
PUB = datetime(2026, 3, 2, tzinfo=timezone.utc)


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


@pytest.fixture(autouse=True)
def _stub_image_fetch(monkeypatch):
    """Write a placeholder file instead of hitting the network."""
    def fake_fetch(url: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"IMG:" + url.encode())
        return True

    monkeypatch.setattr(worker, "_fetch_image", fake_fetch)
    return fake_fetch


def _seed(db, *, thumbnail_url="http://img/avatar.jpg", banner_url="http://img/banner.jpg"):
    db.add(User(id=1, oidc_sub="s1", email="u1@e", download_enabled=True, keep_last_n=15))
    db.add(Channel(channel_id=CHAN, title=CHAN_TITLE,
                   thumbnail_url=thumbnail_url, banner_url=banner_url))
    db.add(Subscription(user_id=1, channel_id=CHAN, ignored=False))
    db.commit()


def _complete_download(db, media_root, vid="vid00000001", epnum=1) -> Path:
    video = Video(video_id=vid, channel_id=CHAN, title="Ep One", published_at=PUB)
    db.add(video)
    db.commit()
    canon_dir = naming.canonical_episode_dir(media_root, CHAN_TITLE, PUB)
    basename = naming.episode_basename(CHAN_TITLE, PUB, epnum, video.title)
    canon_dir.mkdir(parents=True, exist_ok=True)
    mkv = canon_dir / f"{basename}.mkv"
    mkv.write_bytes(b"video-bytes")
    (canon_dir / f"{basename}.nfo").write_bytes(b"<episodedetails/>")
    (canon_dir / f"{basename}-thumb.jpg").write_bytes(b"jpg")
    db.add(Download(video_id=vid, status="complete", file_path=str(mkv),
                    file_size_bytes=mkv.stat().st_size,
                    completed_at=datetime.now(timezone.utc)))
    db.commit()
    return mkv


# --- sync_library_art ----------------------------------------------------

def test_sync_places_series_and_season_posters(db, tmp_path):
    media = str(tmp_path)
    _seed(db)
    _complete_download(db, media)
    reconcile_links(db, media)

    created = sync_library_art(db, media)
    assert created == 3  # channel poster + season poster + channel backdrop

    # Canonical art (one inode) exists.
    canon = naming.canonical_channel_dir(media, CHAN_TITLE)
    assert (canon / naming.POSTER_NAME).exists()
    assert (canon / naming.BACKDROP_NAME).exists()

    # User library: series folder gets poster + backdrop, season folder a poster.
    season_dir = naming.user_episode_dir(media, 1, CHAN_TITLE, PUB)
    channel_dir = season_dir.parent
    assert (channel_dir / naming.POSTER_NAME).exists()
    assert (channel_dir / naming.BACKDROP_NAME).exists()
    assert (season_dir / naming.POSTER_NAME).exists()

    # Shared inode, not a copy.
    assert (channel_dir / naming.POSTER_NAME).samefile(canon / naming.POSTER_NAME)


def test_sync_is_idempotent(db, tmp_path):
    media = str(tmp_path)
    _seed(db)
    _complete_download(db, media)
    reconcile_links(db, media)

    assert sync_library_art(db, media) == 3
    assert sync_library_art(db, media) == 0  # nothing new the second time


def test_sync_skips_channel_without_art(db, tmp_path):
    media = str(tmp_path)
    _seed(db, thumbnail_url=None, banner_url=None)
    _complete_download(db, media)
    reconcile_links(db, media)

    assert sync_library_art(db, media) == 0
    canon = naming.canonical_channel_dir(media, CHAN_TITLE)
    assert not (canon / naming.POSTER_NAME).exists()


def test_sync_places_poster_without_banner(db, tmp_path):
    media = str(tmp_path)
    _seed(db, banner_url=None)
    _complete_download(db, media)
    reconcile_links(db, media)

    created = sync_library_art(db, media)
    assert created == 2  # channel poster + season poster, no backdrop
    canon = naming.canonical_channel_dir(media, CHAN_TITLE)
    assert (canon / naming.POSTER_NAME).exists()
    assert not (canon / naming.BACKDROP_NAME).exists()


# --- teardown: art files must not keep empty dirs alive -------------------

def test_prune_removes_dirs_that_hold_only_posters(db, tmp_path):
    media = str(tmp_path)
    _seed(db)
    _complete_download(db, media)
    reconcile_links(db, media)
    sync_library_art(db, media)

    # Drop the subscription so nothing is retained, then reconcile + prune.
    db.query(Subscription).delete()
    db.commit()
    reconcile_links(db, media)   # removes the user's hardlinks (and their art)
    run_prune(db)                # removes canonical files (and their art)

    # No empty series/season shells left behind in either tree: the user's
    # channel dir and the canonical channel dir (which held only posters) go.
    user_channel_dir = naming.user_episode_dir(media, 1, CHAN_TITLE, PUB).parent
    assert not user_channel_dir.exists()
    assert not naming.canonical_channel_dir(media, CHAN_TITLE).exists()


# --- backfill_channel_art ------------------------------------------------

def test_backfill_stores_probed_urls(db, tmp_path, monkeypatch):
    media = str(tmp_path)
    # Channel present, one completed download, but no art URLs yet.
    db.add(User(id=1, oidc_sub="s1", email="u1@e", download_enabled=True))
    db.add(Channel(channel_id=CHAN, title=CHAN_TITLE))
    db.add(Subscription(user_id=1, channel_id=CHAN, ignored=False))
    db.commit()
    _complete_download(db, media)

    monkeypatch.setattr(
        ytdlp, "probe_channel",
        lambda cid: ytdlp.ChannelArt(avatar_url="http://a/av.jpg",
                                     banner_url="http://a/bn.jpg"),
    )
    assert backfill_channel_art(db) == 1
    channel = db.get(Channel, CHAN)
    assert channel.thumbnail_url == "http://a/av.jpg"
    assert channel.banner_url == "http://a/bn.jpg"


def test_backfill_records_sentinel_when_no_art(db, tmp_path, monkeypatch):
    media = str(tmp_path)
    db.add(Channel(channel_id=CHAN, title=CHAN_TITLE))
    db.commit()
    _complete_download(db, media)

    monkeypatch.setattr(
        ytdlp, "probe_channel",
        lambda cid: ytdlp.ChannelArt(avatar_url=None, banner_url=None),
    )
    backfill_channel_art(db)
    channel = db.get(Channel, CHAN)
    # "" sentinel means "probed, none" so it isn't re-probed forever.
    assert channel.thumbnail_url == ""
    # And a second pass finds nothing left to probe.
    assert backfill_channel_art(db) == 0


def test_backfill_ignores_channels_without_downloads(db, monkeypatch):
    db.add(Channel(channel_id=CHAN, title=CHAN_TITLE))
    db.commit()
    called = []
    monkeypatch.setattr(
        ytdlp, "probe_channel",
        lambda cid: called.append(cid) or ytdlp.ChannelArt(None, None),
    )
    assert backfill_channel_art(db) == 0
    assert called == []  # never probed a channel we don't archive
