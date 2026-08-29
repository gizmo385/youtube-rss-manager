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

from types import SimpleNamespace

from youtube_subs_opml.downloader import naming, worker
from youtube_subs_opml.downloader.worker import backfill_audio, reconcile_links, run_prune
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
    VideoShort,
)
from youtube_subs_opml.web.services.archive import (
    channel_intents,
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


# --- Shorts are excluded from the archive when the channel excludes them ----

def _mark_short(db, vid):
    db.add(VideoShort(video_id=vid, is_short=True))
    db.commit()


def test_retention_excludes_known_shorts_when_shorts_off(db):
    add_user(db, 1, download_enabled=True, keep_last_n=0, include_shorts=False)
    add_sub(db, 1)
    ids = add_videos(db, 3)
    _mark_short(db, ids[1])

    assert user_retained_video_ids(db) == {1: {ids[0], ids[2]}}  # the Short dropped


def test_retention_keeps_shorts_when_shorts_on(db):
    add_user(db, 1, download_enabled=True, keep_last_n=0, include_shorts=True)
    add_sub(db, 1)
    ids = add_videos(db, 3)
    _mark_short(db, ids[1])

    assert user_retained_video_ids(db) == {1: set(ids)}  # kept — user wants Shorts


def test_shorts_do_not_consume_keep_slots(db):
    add_user(db, 1, download_enabled=True, keep_last_n=2, include_shorts=False)
    add_sub(db, 1)
    ids = add_videos(db, 4)  # ids[3] newest
    _mark_short(db, ids[3])  # newest is a Short

    # Without excluding first, the top-2 would be {short, ids[2]} → {ids[2]}.
    # Excluding first keeps the two most recent *regular* uploads.
    assert user_retained_video_ids(db) == {1: {ids[2], ids[1]}}


def test_channel_intent_include_shorts_is_a_union(db):
    add_user(db, 1, download_enabled=True, include_shorts=False)
    add_user(db, 2, download_enabled=True, include_shorts=True)
    add_sub(db, 1)
    add_sub(db, 2)
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].include_shorts is True  # user 2 wants them


def test_channel_intent_shorts_false_when_no_downloader_wants_them(db):
    add_user(db, 1, download_enabled=True, include_shorts=False)
    add_user(db, 2, download_enabled=False, include_shorts=True)  # wants shorts but no download
    add_sub(db, 1)
    add_sub(db, 2)
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].include_shorts is False


def test_worker_skips_short_when_channel_excludes_shorts(db, monkeypatch):
    add_user(db, 1, download_enabled=True, include_shorts=False)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    row = Download(video_id=ids[0], status="downloading")
    db.add(row)
    db.commit()

    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        worker.ytdlp, "probe",
        lambda vid: SimpleNamespace(duration_seconds=30, title="t", description="d"),
    )
    monkeypatch.setattr(worker, "classify_videos", lambda video_ids, db: {video_ids[0]: True})

    def _no_download(*a, **k):
        raise AssertionError("must not download a Short the channel excludes")

    monkeypatch.setattr(worker.ytdlp, "download", _no_download)

    worker.process(row, db)

    assert row.status == "skipped"
    assert row.skip_reason == "short"


# --- terminal probe errors skip immediately instead of retrying ----------

def test_terminal_skip_reason_classifies_permanent_errors():
    geo = ("ERROR: [youtube] kYjRBnFKNN4: The uploader has not made this video "
           "available in your country")
    assert worker._terminal_skip_reason(geo) == "geo_blocked"
    assert worker._terminal_skip_reason("ERROR: Private video. Sign in if you've been granted access") == "unavailable"
    assert worker._terminal_skip_reason("This video is available to this channel's members") == "members_only"
    # Bot-detection / throttling is transient — must NOT be treated as terminal.
    assert worker._terminal_skip_reason(
        "ERROR: Sign in to confirm you're not a bot") is None
    assert worker._terminal_skip_reason("HTTP Error 429: Too Many Requests") is None


def test_worker_skips_geoblocked_video_on_first_probe(db, monkeypatch):
    add_user(db, 1, download_enabled=True)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    row = Download(video_id=ids[0], status="downloading", attempts=1)  # first attempt
    db.add(row)
    db.commit()

    def _geo_block(vid, **kw):
        raise worker.ytdlp.ProbeError(
            "ERROR: [youtube] x: The uploader has not made this video available "
            "in your country")

    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(worker.ytdlp, "probe", _geo_block)

    worker.process(row, db)

    # Skipped straight away — not left pending for four more back-off'd retries.
    assert row.status == "skipped"
    assert row.skip_reason == "geo_blocked"
    assert row.next_attempt_at is None


def test_backfill_classifies_and_prunes_archived_shorts(db, tmp_path, monkeypatch):
    from youtube_subs_opml.web.services import shorts

    media = str(tmp_path)
    add_user(db, 1, download_enabled=True, keep_last_n=0, include_shorts=False)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    canon = [complete_download(db, media, v, i + 1) for i, v in enumerate(ids)]
    reconcile_links(db, media)  # both linked — not yet known to be Shorts

    # ids[0] is really a Short; drive classify_videos via a mocked probe so the
    # real caching path runs (a verdict row is written for each).
    monkeypatch.setattr(shorts, "_probe_is_short", lambda vid, client: vid == ids[0])

    assert worker.backfill_shorts(db) == 1
    assert db.get(VideoShort, ids[0]).is_short is True
    assert db.get(VideoShort, ids[1]).is_short is False

    # The now-known Short falls out of retention: its link is removed and its
    # canonical file pruned; the regular upload stays.
    reconcile_links(db, media)
    assert run_prune(db) == 1
    assert not canon[0].exists()
    assert canon[1].exists()


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


# --- empty-directory cleanup (no phantom empty series in Jellyfin) -------

def test_reconcile_removes_empty_user_channel_dir(db, tmp_path):
    media = str(tmp_path)
    user = add_user(db, 1, download_enabled=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    [complete_download(db, media, v, i + 1) for i, v in enumerate(ids)]
    reconcile_links(db, media)

    chan_dir = naming.user_episode_dir(media, 1, CHAN_TITLE, BASE).parent
    assert chan_dir.is_dir()  # present while episodes are linked

    # Stop archiving the channel: the user retains nothing for it.
    user.download_enabled = False
    db.commit()
    reconcile_links(db, media)

    # The emptied Season/Channel dirs are gone, so Jellyfin won't keep showing a
    # phantom empty series — but the user's library root stays put.
    assert not chan_dir.exists()
    assert Path(media, naming.LIBRARIES_SUBDIR, "1").is_dir()


def test_reconcile_keeps_channel_dir_with_remaining_episode(db, tmp_path):
    media = str(tmp_path)
    user = add_user(db, 1, download_enabled=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    [complete_download(db, media, v, i + 1) for i, v in enumerate(ids)]
    reconcile_links(db, media)

    user.keep_last_n = 1  # drop only the oldest
    db.commit()
    reconcile_links(db, media)

    chan_dir = naming.user_episode_dir(media, 1, CHAN_TITLE, BASE).parent
    assert chan_dir.is_dir()               # a season still holds the newest
    assert list(chan_dir.rglob("*.mkv"))   # and its file


def test_prune_removes_empty_canonical_channel_dir(db, tmp_path):
    media = str(tmp_path)
    user = add_user(db, 1, download_enabled=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    [complete_download(db, media, v, i + 1) for i, v in enumerate(ids)]
    reconcile_links(db, media)

    canon_chan = naming.canonical_episode_dir(media, CHAN_TITLE, BASE).parent
    assert canon_chan.is_dir()

    user.download_enabled = False
    db.commit()
    reconcile_links(db, media)     # drop the hardlinks first
    assert run_prune(db) == 2      # both canonical files pruned

    assert not canon_chan.exists()                       # empty channel dir removed
    assert Path(media, naming.CANONICAL_SUBDIR).is_dir()  # canonical root kept


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


# --- podcast audio backfill ----------------------------------------------

def _fake_extract_audio(video_id, output_path, *, sleep_interval=5, timeout=3600):
    """Stand-in for ytdlp.extract_audio: writes a .m4a next to output_path."""
    p = Path(str(output_path)).with_suffix(".m4a")
    p.write_bytes(b"audio-bytes-1234")
    return p


def test_backfill_audio_extracts_for_podcast_channels(db, tmp_path, monkeypatch):
    media_root = str(tmp_path)
    add_user(db, 1, download_enabled=True, generate_podcast=True)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    complete_download(db, media_root, ids[0], 1)
    complete_download(db, media_root, ids[1], 2)
    monkeypatch.setattr(worker.ytdlp, "extract_audio", _fake_extract_audio)

    n = backfill_audio(db, SimpleNamespace(ytdlp_sleep_interval=0))

    assert n == 2
    d0 = db.get(Download, ids[0])
    assert d0.audio_path.endswith(".m4a")
    assert d0.audio_size_bytes == len(b"audio-bytes-1234")
    # Idempotent: a second pass finds nothing left to do.
    assert backfill_audio(db, SimpleNamespace(ytdlp_sleep_interval=0)) == 0


def test_backfill_audio_skips_non_podcast_channels(db, tmp_path, monkeypatch):
    media_root = str(tmp_path)
    add_user(db, 1, download_enabled=True, generate_podcast=False)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    complete_download(db, media_root, ids[0], 1)
    called = []
    monkeypatch.setattr(
        worker.ytdlp, "extract_audio", lambda *a, **k: called.append(1)
    )

    assert backfill_audio(db, SimpleNamespace(ytdlp_sleep_interval=0)) == 0
    assert not called
    assert db.get(Download, ids[0]).audio_path is None


def test_backfill_audio_failure_is_nonfatal(db, tmp_path, monkeypatch):
    from youtube_subs_opml.downloader import ytdlp

    media_root = str(tmp_path)
    add_user(db, 1, download_enabled=True, generate_podcast=True)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    complete_download(db, media_root, ids[0], 1)

    def boom(*a, **k):
        raise ytdlp.DownloadError("throttled")
    monkeypatch.setattr(worker.ytdlp, "extract_audio", boom)

    assert backfill_audio(db, SimpleNamespace(ytdlp_sleep_interval=0)) == 0
    assert db.get(Download, ids[0]).audio_path is None  # left for a later pass
