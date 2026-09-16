"""Date-based episode numbering and the one-off renumber migration.

Runs against in-memory SQLite with a real temp media tree so the file renames
and NFO rewrites are exercised for real.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.downloader import naming, worker
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    Channel,
    Download,
    DownloadLink,
    User,
    Video,
)

CHAN = "UCchannel0000000000000"
CHAN_TITLE = "Test Chan"
PUB = datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc)  # -> MMDD 315 -> E0315


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, oidc_sub="s1", email="u1@e"))
    session.add(Channel(channel_id=CHAN, title=CHAN_TITLE))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_date_episode_number_encodes_mmdd():
    assert naming.date_episode_number(PUB) == 315
    assert naming.date_episode_number(datetime(2026, 1, 5, tzinfo=timezone.utc)) == 105
    assert naming.date_episode_number(datetime(2026, 12, 31, tzinfo=timezone.utc)) == 1231
    assert naming.date_episode_number(None) == 0


def test_episode_basename_zero_pads_to_four_digits():
    name = naming.episode_basename(CHAN_TITLE, PUB, 315, "Ep One")
    assert name == "Test Chan - S2026E0315 - Ep One"


def _seed_old_style_episode(db: Session, media_root: str) -> Path:
    """A completed download named with the old sequential (E07) scheme."""
    canon_dir = naming.canonical_episode_dir(media_root, CHAN_TITLE, PUB)
    canon_dir.mkdir(parents=True, exist_ok=True)
    old_stem = f"{CHAN_TITLE} - S2026E07 - Ep One"
    mkv = canon_dir / f"{old_stem}.mkv"
    mkv.write_bytes(b"video-bytes")
    nfo = naming.build_nfo(
        title="Ep One", channel_title=CHAN_TITLE, published_at=PUB,
        episode_number=7, description="Cool description", video_id="vid00000001",
    )
    (canon_dir / f"{old_stem}.nfo").write_bytes(nfo)
    (canon_dir / f"{old_stem}-thumb.jpg").write_bytes(b"jpg")

    db.add(Video(video_id="vid00000001", channel_id=CHAN, title="Ep One",
                 published_at=PUB))
    db.add(Download(video_id="vid00000001", status="complete", file_path=str(mkv)))

    # A stale per-user hardlink under the old name, which the migration drops.
    user_dir = naming.user_episode_dir(media_root, 1, CHAN_TITLE, PUB)
    user_dir.mkdir(parents=True, exist_ok=True)
    link = user_dir / f"{old_stem}.mkv"
    link.write_bytes(b"video-bytes")
    db.add(DownloadLink(user_id=1, video_id="vid00000001", link_path=str(link)))
    db.commit()
    return mkv


def test_renumber_renames_files_patches_nfo_and_backfills_description(db, tmp_path):
    media_root = str(tmp_path)
    old_mkv = _seed_old_style_episode(db, media_root)

    renamed = worker.renumber_episodes(db, media_root)
    assert renamed == 1

    canon_dir = old_mkv.parent
    new_stem = f"{CHAN_TITLE} - S2026E0315 - Ep One"
    # Files renamed, old names gone, sidecars carried along.
    assert not old_mkv.exists()
    assert (canon_dir / f"{new_stem}.mkv").exists()
    assert (canon_dir / f"{new_stem}.nfo").exists()
    assert (canon_dir / f"{new_stem}-thumb.jpg").exists()

    download = db.get(Download, "vid00000001")
    assert download.file_path == str(canon_dir / f"{new_stem}.mkv")

    # NFO <episode> patched to the date-based number.
    root = ET.parse(canon_dir / f"{new_stem}.nfo").getroot()
    assert root.find("episode").text == "315"

    # Description backfilled from the NFO's <plot> so podcast notes work.
    assert db.get(Video, "vid00000001").description == "Cool description"

    # Stale hardlink and its DownloadLink row removed; reconcile rebuilds later.
    assert db.query(DownloadLink).count() == 0
    assert not (naming.user_episode_dir(media_root, 1, CHAN_TITLE, PUB)
                / f"{CHAN_TITLE} - S2026E07 - Ep One.mkv").exists()


def test_renumber_follows_the_extracted_audio(db, tmp_path):
    """The .m4a is renamed with the rest of the stem, so audio_path must move too.

    A stale audio_path is what a podcast client sees as an episode that plays
    404 — the feed still lists it, but the enclosure points at a dead name.
    """
    media_root = str(tmp_path)
    old_mkv = _seed_old_style_episode(db, media_root)
    old_m4a = old_mkv.with_suffix(".m4a")
    old_m4a.write_bytes(b"audio-bytes")
    download = db.get(Download, "vid00000001")
    download.audio_path = str(old_m4a)
    download.audio_size_bytes = old_m4a.stat().st_size
    db.commit()

    worker.renumber_episodes(db, media_root)

    new_m4a = old_mkv.parent / f"{CHAN_TITLE} - S2026E0315 - Ep One.m4a"
    assert new_m4a.exists() and not old_m4a.exists()
    assert db.get(Download, "vid00000001").audio_path == str(new_m4a)


def test_repair_repoints_audio_left_behind_by_an_earlier_renumber(db, tmp_path):
    """Archives migrated before the fix have a stale audio_path and no rename left
    to do — the repair pass reconciles the column against the disk instead."""
    media_root = str(tmp_path)
    old_mkv = _seed_old_style_episode(db, media_root)
    download = db.get(Download, "vid00000001")
    download.audio_path = str(old_mkv.with_suffix(".m4a"))  # pre-rename name
    db.commit()

    # Renumber first, writing the audio out under its post-rename name only.
    worker.renumber_episodes(db, media_root)
    new_mkv = Path(db.get(Download, "vid00000001").file_path)
    new_m4a = new_mkv.with_suffix(".m4a")
    new_m4a.write_bytes(b"audio-bytes")
    download = db.get(Download, "vid00000001")
    download.audio_path = str(old_mkv.with_suffix(".m4a"))
    db.commit()

    assert worker.repair_audio_paths(db) == 1

    download = db.get(Download, "vid00000001")
    assert download.audio_path == str(new_m4a)
    assert download.audio_size_bytes == len(b"audio-bytes")


def test_repair_clears_audio_that_is_really_gone(db, tmp_path):
    """No file to point at: clear the column so backfill_audio re-extracts it."""
    media_root = str(tmp_path)
    old_mkv = _seed_old_style_episode(db, media_root)
    download = db.get(Download, "vid00000001")
    download.audio_path = str(old_mkv.with_suffix(".m4a"))  # never existed
    download.audio_size_bytes = 10
    db.commit()

    assert worker.repair_audio_paths(db) == 1

    download = db.get(Download, "vid00000001")
    assert download.audio_path is None
    assert download.audio_size_bytes is None
    assert download.status == "complete"  # the video is still there


def test_repair_requeues_an_audio_only_row_with_nothing_left(db, tmp_path):
    """An audio-only download has no video for backfill_audio to work from, so a
    missing .m4a means the row goes back through the queue."""
    db.add(Video(video_id="vid00000002", channel_id=CHAN, title="Audio Only",
                 published_at=PUB))
    db.add(Download(video_id="vid00000002", status="complete", file_path=None,
                    audio_path=str(tmp_path / "gone.m4a"), attempts=3))
    db.commit()

    assert worker.repair_audio_paths(db) == 1

    download = db.get(Download, "vid00000002")
    assert download.audio_path is None
    assert download.status == "pending"
    assert download.attempts == 0


def test_repair_leaves_present_audio_alone(db, tmp_path):
    media_root = str(tmp_path)
    old_mkv = _seed_old_style_episode(db, media_root)
    m4a = old_mkv.with_suffix(".m4a")
    m4a.write_bytes(b"audio-bytes")
    download = db.get(Download, "vid00000001")
    download.audio_path = str(m4a)
    db.commit()

    assert worker.repair_audio_paths(db) == 0


def test_renumber_is_idempotent(db, tmp_path):
    media_root = str(tmp_path)
    _seed_old_style_episode(db, media_root)
    assert worker.renumber_episodes(db, media_root) == 1
    # Second pass: everything already matches, nothing to rename.
    assert worker.renumber_episodes(db, media_root) == 0
