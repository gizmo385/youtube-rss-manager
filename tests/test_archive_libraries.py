"""Per-user library tests: retention resolution, hardlink reconcile, prune.

Runs against in-memory SQLite with a real temp media tree, so the hardlink and
inode behaviour is exercised for real — no Postgres, Keycloak, or network.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.downloader import naming
from youtube_subs_opml.downloader.worker import reconcile_links, run_prune
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
    Download,
    DownloadLink,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.services.archive import (
    retained_video_ids,
    user_retained_video_ids,
)

CHAN = "UCchannel0000000000000"
CHAN_TITLE = "Test Chan"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Channel(channel_id=CHAN, title=CHAN_TITLE))
    session.commit()
    try:
        yield session
    finally:
        session.close()


# --- seeding helpers -----------------------------------------------------

def add_user(db: Session, uid: int, **prefs) -> User:
    defaults = dict(
        oidc_sub=f"sub{uid}",
        email=f"u{uid}@e",
        download_enabled=False,
        keep_last_n=15,
        max_duration_seconds=0,
        generate_podcast=False,
        link_target="youtube",
    )
    defaults.update(prefs)
    user = User(id=uid, **defaults)
    db.add(user)
    db.commit()
    return user


def add_sub(db: Session, uid: int, **prefs) -> Subscription:
    sub = Subscription(user_id=uid, channel_id=CHAN, ignored=False, **prefs)
    db.add(sub)
    db.commit()
    return sub


def add_videos(db: Session, n: int) -> list[str]:
    """n videos, oldest first. Returns ids in publish order (v0 oldest)."""
    ids = []
    for i in range(n):
        vid = f"vid{i:08d}"
        db.add(
            Video(
                video_id=vid,
                channel_id=CHAN,
                title=f"Episode {i}",
                published_at=BASE + timedelta(days=i),
            )
        )
        ids.append(vid)
    db.commit()
    return ids


def complete_download(db: Session, media_root: str, vid: str, epnum: int) -> Path:
    """Write a canonical .mkv (+ sidecars) and a completed Download row."""
    video = db.get(Video, vid)
    canon_dir = naming.canonical_episode_dir(media_root, CHAN_TITLE, video.published_at)
    basename = naming.episode_basename(CHAN_TITLE, video.published_at, epnum, video.title)
    canon_dir.mkdir(parents=True, exist_ok=True)
    mkv = canon_dir / f"{basename}.mkv"
    mkv.write_bytes(b"video-bytes")
    (canon_dir / f"{basename}.nfo").write_bytes(b"<episodedetails/>")
    (canon_dir / f"{basename}-thumb.jpg").write_bytes(b"jpg")
    db.add(
        Download(
            video_id=vid,
            status="complete",
            file_path=str(mkv),
            file_size_bytes=mkv.stat().st_size,
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return mkv


# --- retention resolution ------------------------------------------------

def test_per_user_retention_respects_keep_last_n(db):
    add_user(db, 1, download_enabled=True, keep_last_n=2)
    add_sub(db, 1)
    ids = add_videos(db, 3)  # ids[2] newest

    retained = user_retained_video_ids(db)
    assert retained == {1: {ids[2], ids[1]}}  # two most recent


def test_user_with_download_off_contributes_nothing(db):
    add_user(db, 1, download_enabled=True, keep_last_n=1)
    add_user(db, 2, download_enabled=False, keep_last_n=15)
    add_sub(db, 1)
    add_sub(db, 2)
    ids = add_videos(db, 3)

    retained = user_retained_video_ids(db)
    assert retained == {1: {ids[2]}}  # user 2 absent entirely
    # Union: only what an enabling user wants — user 2's large keep can't inflate it.
    assert retained_video_ids(db) == {ids[2]}


def test_category_overrides_user_default(db):
    # User default is download OFF, but a category turns it on with unlimited keep.
    add_user(db, 1, download_enabled=False, keep_last_n=1)
    add_sub(db, 1)
    db.add(Category(id=10, user_id=1, name="Keep", slug="keep",
                    download_enabled=True, keep_last_n=0))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.commit()
    ids = add_videos(db, 3)

    assert user_retained_video_ids(db) == {1: set(ids)}  # 0 == unlimited


def test_subscription_overrides_category(db):
    add_user(db, 1, download_enabled=True, keep_last_n=15)
    # Subscription explicitly disables download; category says enable.
    add_sub(db, 1, download_enabled=False)
    db.add(Category(id=10, user_id=1, name="Keep", slug="keep", download_enabled=True))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.commit()
    add_videos(db, 3)

    assert user_retained_video_ids(db) == {}  # subscription wins → off


# --- reconcile: hardlink fan-out ----------------------------------------

def test_reconcile_creates_hardlinks(db, tmp_path):
    media = str(tmp_path)
    add_user(db, 1, download_enabled=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    canon = [complete_download(db, media, v, i + 1) for i, v in enumerate(ids)]

    created, removed = reconcile_links(db, media)
    assert (created, removed) == (2, 0)

    links = db.query(DownloadLink).all()
    assert {l.video_id for l in links} == set(ids)
    for link, canon_mkv in zip(sorted(links, key=lambda l: l.video_id), canon):
        lp = Path(link.link_path)
        assert lp.exists()
        # Same inode → one physical copy, two names.
        assert os.stat(lp).st_ino == os.stat(canon_mkv).st_ino
        assert os.stat(lp).st_nlink == 2
        # Sidecars linked too.
        assert (lp.parent / (lp.stem + ".nfo")).exists()
        assert (lp.parent / (lp.stem + "-thumb.jpg")).exists()
        # Landed under libraries/{user_id}, not the canonical tree.
        assert naming.LIBRARIES_SUBDIR in lp.parts
        assert naming.CANONICAL_SUBDIR not in lp.parts


def test_reconcile_is_idempotent(db, tmp_path):
    media = str(tmp_path)
    add_user(db, 1, download_enabled=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    complete_download(db, media, ids[0], 1)

    assert reconcile_links(db, media) == (1, 0)
    assert reconcile_links(db, media) == (0, 0)  # nothing to do the second time


def test_reconcile_removes_links_when_no_longer_retained(db, tmp_path):
    media = str(tmp_path)
    user = add_user(db, 1, download_enabled=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    canon = [complete_download(db, media, v, i + 1) for i, v in enumerate(ids)]
    reconcile_links(db, media)

    # Tighten retention to just the newest video.
    user.keep_last_n = 1
    db.commit()
    created, removed = reconcile_links(db, media)
    assert (created, removed) == (0, 1)

    remaining = {l.video_id for l in db.query(DownloadLink).all()}
    assert remaining == {ids[1]}  # only newest kept
    # The dropped user's hardlink is gone, but the canonical file survives.
    assert not any(
        p.exists() for p in Path(media, naming.LIBRARIES_SUBDIR).rglob("*.mkv")
        if ids[0] in p.name or "Episode 0" in p.name
    )
    assert canon[0].exists()  # canonical untouched by reconcile


# --- prune: canonical teardown ------------------------------------------

def test_prune_removes_only_unwanted_canonical(db, tmp_path):
    media = str(tmp_path)
    add_user(db, 1, download_enabled=True, keep_last_n=1)  # keeps only newest
    add_sub(db, 1)
    ids = add_videos(db, 2)
    canon_old = complete_download(db, media, ids[0], 1)
    canon_new = complete_download(db, media, ids[1], 2)

    reconcile_links(db, media)  # links only the newest
    pruned = run_prune(db)
    assert pruned == 1

    assert not canon_old.exists()             # unwanted canonical gone
    assert db.get(Download, ids[0]) is None    # row deleted
    assert canon_new.exists()                  # retained canonical kept
    assert db.get(Download, ids[1]) is not None


def test_prune_keeps_file_a_second_user_still_wants(db, tmp_path):
    media = str(tmp_path)
    add_user(db, 1, download_enabled=True, keep_last_n=1)   # wants newest only
    add_user(db, 2, download_enabled=True, keep_last_n=0)   # wants everything
    add_sub(db, 1)
    add_sub(db, 2)
    ids = add_videos(db, 2)
    canon_old = complete_download(db, media, ids[0], 1)
    complete_download(db, media, ids[1], 2)

    reconcile_links(db, media)
    assert run_prune(db) == 0  # user 2 still wants the old one
    assert canon_old.exists()

    links_old = {l.user_id for l in db.query(DownloadLink)
                 .filter_by(video_id=ids[0]).all()}
    assert links_old == {2}  # only user 2 has the old video linked


# --- hardlink primitive + sidecar discovery ------------------------------

def test_hardlink_idempotent_and_last_link_frees_data(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"data")
    dst = tmp_path / "a" / "src.mkv"

    naming.hardlink(src, dst)
    naming.hardlink(src, dst)  # second call must not raise
    assert os.stat(src).st_nlink == 2
    assert os.stat(dst).st_ino == os.stat(src).st_ino

    dst2 = tmp_path / "b" / "src.mkv"
    naming.hardlink(src, dst2)
    assert os.stat(src).st_nlink == 3

    dst.unlink()
    dst2.unlink()
    assert os.stat(src).st_nlink == 1  # data still there via canonical
    src.unlink()
    assert not src.exists()


def test_hardlink_replaces_stale_target(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"new")
    dst = tmp_path / "dst.mkv"
    dst.write_bytes(b"stale-different-inode")

    naming.hardlink(src, dst)  # dst points elsewhere → must be replaced
    assert os.stat(dst).st_ino == os.stat(src).st_ino
    assert dst.read_bytes() == b"new"


def test_media_is_ready_requires_mount_or_optin(tmp_path, monkeypatch):
    store = tmp_path / "media"
    store.mkdir()

    # A plain writable dir is NOT ready by default (could be ephemeral).
    assert naming.media_is_ready(str(store)) is False
    # ...but the local-dev opt-in accepts it.
    assert naming.media_is_ready(str(store), allow_unmounted=True) is True
    # A real mount point is ready even without the opt-in.
    monkeypatch.setattr(naming.os.path, "ismount", lambda p: True)
    assert naming.media_is_ready(str(store)) is True


def test_media_is_ready_false_for_missing_dir(tmp_path):
    assert naming.media_is_ready(str(tmp_path / "nope")) is False
    assert naming.media_is_ready(str(tmp_path / "nope"), allow_unmounted=True) is False


def test_episode_files_matches_sidecars_not_prefix_siblings(tmp_path):
    d = tmp_path
    mkv = d / "Chan - S2026E07 - Title.mkv"
    mkv.write_bytes(b"")
    (d / "Chan - S2026E07 - Title.nfo").write_bytes(b"")
    (d / "Chan - S2026E07 - Title-thumb.jpg").write_bytes(b"")
    # A different episode whose name is NOT a boundary-match of the stem.
    (d / "Chan - S2026E08 - Title Two.mkv").write_bytes(b"")

    names = {p.name for p in naming.episode_files(mkv)}
    assert names == {
        "Chan - S2026E07 - Title.mkv",
        "Chan - S2026E07 - Title.nfo",
        "Chan - S2026E07 - Title-thumb.jpg",
    }
