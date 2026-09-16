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
from youtube_subs_opml.web.services.prefs import meets_duration_floor
from youtube_subs_opml.web.services.archive import (
    channel_intents,
    enqueue_pending,
    retained_podcast_ids,
    retained_video_ids,
    user_retained_podcast_ids,
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


# --- minimum duration ----------------------------------------------------

def test_duration_floor_defaults_to_letting_everything_through(db):
    add_user(db, 1, download_enabled=True)
    add_sub(db, 1)
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].min_duration_seconds == 0
    assert meets_duration_floor(5, 0) is True


def test_duration_floor_boundaries():
    assert meets_duration_floor(300, 300) is True   # exactly at the floor
    assert meets_duration_floor(299, 300) is False
    assert meets_duration_floor(None, 300) is True  # unknown length fails open


def test_lowest_floor_wins_across_subscribers(db):
    """Permissive for a floor is the *smallest* number, mirroring how the
    ceiling takes the largest — one user's strict filter can't hide a video
    another user wants."""
    add_user(db, 1, download_enabled=True, min_duration_seconds=600)
    add_user(db, 2, download_enabled=True, min_duration_seconds=60)
    add_sub(db, 1)
    add_sub(db, 2)
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].min_duration_seconds == 60


def test_no_floor_beats_a_floor_across_subscribers(db):
    add_user(db, 1, download_enabled=True, min_duration_seconds=600)
    add_user(db, 2, download_enabled=True, min_duration_seconds=0)
    add_sub(db, 1)
    add_sub(db, 2)
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].min_duration_seconds == 0


def test_subscription_floor_overrides_category_and_user(db):
    add_user(db, 1, download_enabled=True, min_duration_seconds=600)
    add_sub(db, 1, min_duration_seconds=120)
    db.add(Category(id=10, user_id=1, name="Long", slug="long",
                    min_duration_seconds=900))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.commit()
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].min_duration_seconds == 120


def test_least_restrictive_floor_across_categories(db):
    add_user(db, 1, download_enabled=True, min_duration_seconds=900)
    add_sub(db, 1)
    db.add(Category(id=10, user_id=1, name="A", slug="a", min_duration_seconds=600))
    db.add(Category(id=11, user_id=1, name="B", slug="b", min_duration_seconds=120))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=11))
    db.commit()
    add_videos(db, 1)

    assert channel_intents(db)[CHAN].min_duration_seconds == 120


def test_worker_skips_a_video_under_the_duration_floor(db, monkeypatch):
    add_user(db, 1, download_enabled=True, min_duration_seconds=300)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    row = Download(video_id=ids[0], status="downloading")
    db.add(row)
    db.commit()

    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        worker.ytdlp, "probe",
        lambda vid: SimpleNamespace(duration_seconds=90, title="t", description="d"),
    )

    def _no_download(*a, **k):
        raise AssertionError("must not download a video under the floor")

    monkeypatch.setattr(worker.ytdlp, "download", _no_download)
    # classify_videos probes YouTube over the network; stub it so the test
    # exercises the duration check and nothing else.
    monkeypatch.setattr(worker, "classify_videos", lambda video_ids, db: {})

    worker.process(row, db)

    assert row.status == "skipped"
    assert row.skip_reason == "too_short"


def test_worker_keeps_a_video_at_exactly_the_floor(db, tmp_path, monkeypatch):
    add_user(db, 1, download_enabled=False, generate_podcast=True,
             min_duration_seconds=300)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    row = Download(video_id=ids[0], status="downloading")
    db.add(row)
    db.commit()

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(media_root=str(tmp_path), ytdlp_format="best",
                                ytdlp_sleep_interval=0, ytdlp_max_retries=1),
    )
    monkeypatch.setattr(
        worker.ytdlp, "probe",
        lambda vid: SimpleNamespace(duration_seconds=300, title="t", description="d"),
    )
    monkeypatch.setattr(worker.ytdlp, "extract_audio", _fake_extract_audio)
    # classify_videos probes YouTube over the network; stub it so the test
    # exercises the duration check and nothing else.
    monkeypatch.setattr(worker, "classify_videos", lambda video_ids, db: {})

    worker.process(row, db)

    assert (row.status, row.skip_reason) == ("complete", None)


def test_worker_fetches_audio_only_for_an_episode_past_the_video_window(db, tmp_path, monkeypatch):
    """The expensive mistake this guards: a wider audio window must not pull a
    500 MiB .mkv that run_prune deletes minutes later."""
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=1, keep_last_n_audio=3)
    add_sub(db, 1)
    ids = add_videos(db, 3)
    row = Download(video_id=ids[0], status="downloading")  # oldest: audio only
    db.add(row)
    db.commit()

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(media_root=str(tmp_path), ytdlp_format="best",
                                ytdlp_sleep_interval=0, ytdlp_max_retries=1),
    )
    monkeypatch.setattr(
        worker.ytdlp, "probe",
        lambda vid: SimpleNamespace(duration_seconds=30, title="t", description="d"),
    )

    def _no_download(*a, **k):
        raise AssertionError("must not fetch video outside the video window")

    monkeypatch.setattr(worker.ytdlp, "download", _no_download)
    monkeypatch.setattr(worker.ytdlp, "extract_audio", _fake_extract_audio)
    # classify_videos probes YouTube over the network; stub it so the test
    # exercises the duration check and nothing else.
    monkeypatch.setattr(worker, "classify_videos", lambda video_ids, db: {})

    worker.process(row, db)

    assert row.status == "complete"
    assert row.file_path is None
    assert row.audio_path.endswith(".m4a")


# --- download spacing ----------------------------------------------------

def test_sleep_between_downloads_disabled_when_zero(monkeypatch):
    slept = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))
    worker._sleep_between_downloads(SimpleNamespace(download_delay_seconds=0))
    assert slept == []


def test_sleep_between_downloads_jitters_delay(monkeypatch):
    slept = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(worker.random, "uniform", lambda a, b: b)  # max jitter
    worker._sleep_between_downloads(SimpleNamespace(download_delay_seconds=5.0))
    assert slept == [10.0]  # base + up-to-base jitter


def test_sleep_between_downloads_skipped_on_shutdown(monkeypatch):
    slept = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(worker, "_shutdown", True)
    worker._sleep_between_downloads(SimpleNamespace(download_delay_seconds=5.0))
    assert slept == []  # stays responsive to SIGTERM


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


# --- separate audio retention --------------------------------------------

def test_audio_window_matches_video_window_when_unset(db):
    """The pre-existing behaviour: one number governs both media kinds."""
    add_user(db, 1, download_enabled=True, generate_podcast=True, keep_last_n=2)
    add_sub(db, 1)
    ids = add_videos(db, 5)

    assert retained_video_ids(db) == {ids[4], ids[3]}
    assert retained_podcast_ids(db) == {ids[4], ids[3]}


def test_audio_window_can_outlive_the_video_window(db):
    """The point of the column: keep 2 videos but 4 podcast episodes."""
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=2, keep_last_n_audio=4)
    add_sub(db, 1)
    ids = add_videos(db, 6)

    assert retained_video_ids(db) == {ids[5], ids[4]}
    assert retained_podcast_ids(db) == {ids[5], ids[4], ids[3], ids[2]}


def test_audio_window_can_be_narrower_than_the_video_window(db):
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=4, keep_last_n_audio=1)
    add_sub(db, 1)
    ids = add_videos(db, 5)

    assert len(retained_video_ids(db)) == 4
    assert retained_podcast_ids(db) == {ids[4]}


def test_audio_window_zero_means_unlimited(db):
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=1, keep_last_n_audio=0)
    add_sub(db, 1)
    ids = add_videos(db, 4)

    assert retained_video_ids(db) == {ids[3]}
    assert retained_podcast_ids(db) == set(ids)


def test_subscription_keep_governs_both_kinds_over_a_global_audio_default(db):
    """Specificity beats media kind: a number typed on the channel wins.

    Otherwise setting a generous global audio default would silently override
    every per-channel window a user had already tuned.
    """
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=2, keep_last_n_audio=10)
    add_sub(db, 1, keep_last_n=1)
    ids = add_videos(db, 6)

    assert retained_video_ids(db) == {ids[5]}
    assert retained_podcast_ids(db) == {ids[5]}


def test_subscription_audio_override_beats_its_own_video_window(db):
    add_user(db, 1, download_enabled=True, generate_podcast=True, keep_last_n=2)
    add_sub(db, 1, keep_last_n=1, keep_last_n_audio=3)
    ids = add_videos(db, 6)

    assert retained_video_ids(db) == {ids[5]}
    assert retained_podcast_ids(db) == {ids[5], ids[4], ids[3]}


def test_category_audio_window_applies(db):
    add_user(db, 1, download_enabled=True, generate_podcast=True, keep_last_n=1)
    add_sub(db, 1)
    db.add(Category(id=10, user_id=1, name="Pods", slug="pods", keep_last_n_audio=3))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.commit()
    ids = add_videos(db, 5)

    assert retained_video_ids(db) == {ids[4]}
    assert retained_podcast_ids(db) == {ids[4], ids[3], ids[2]}


def test_category_video_window_still_shapes_audio_when_it_sets_no_audio(db):
    """A category setting only keep_last_n governs both kinds, as before."""
    add_user(db, 1, download_enabled=True, generate_podcast=True, keep_last_n=1)
    add_sub(db, 1)
    db.add(Category(id=10, user_id=1, name="Deep", slug="deep", keep_last_n=3))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.commit()
    ids = add_videos(db, 5)

    assert retained_podcast_ids(db) == {ids[4], ids[3], ids[2]}


def test_most_permissive_audio_window_wins_across_categories(db):
    """Two categories, one setting audio and one only video: widest wins."""
    add_user(db, 1, download_enabled=True, generate_podcast=True, keep_last_n=1)
    add_sub(db, 1)
    db.add(Category(id=10, user_id=1, name="A", slug="a", keep_last_n_audio=2))
    db.add(Category(id=11, user_id=1, name="B", slug="b", keep_last_n=4))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=10))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=11))
    db.commit()
    ids = add_videos(db, 6)

    assert retained_podcast_ids(db) == {ids[5], ids[4], ids[3], ids[2]}


def test_wider_audio_window_prunes_the_video_but_keeps_the_audio(db, tmp_path):
    """End to end: the .mkv goes, the .m4a stays, the row survives."""
    media_root = str(tmp_path)
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=1, keep_last_n_audio=2)
    add_sub(db, 1)
    ids = add_videos(db, 2)
    mkv = complete_download(db, media_root, ids[0], 1)  # the older one
    complete_download(db, media_root, ids[1], 2)
    audio = mkv.with_suffix(".m4a")
    audio.write_bytes(b"audio")
    d0 = db.get(Download, ids[0])
    d0.audio_path = str(audio)
    d0.audio_size_bytes = audio.stat().st_size
    db.commit()

    run_prune(db)

    d0 = db.get(Download, ids[0])
    assert d0 is not None            # row kept — the audio is still wanted
    assert d0.file_path is None      # video pruned out of its shorter window
    assert not mkv.exists()
    assert audio.exists()            # audio still inside its own window
    assert d0.audio_path == str(audio)


def test_backfill_audio_ignores_episodes_outside_the_audio_window(db, tmp_path, monkeypatch):
    """Otherwise backfill and prune would fight: extract, delete, repeat."""
    media_root = str(tmp_path)
    add_user(db, 1, download_enabled=True, generate_podcast=True,
             keep_last_n=3, keep_last_n_audio=1)
    add_sub(db, 1)
    ids = add_videos(db, 3)
    for i, vid in enumerate(ids):
        complete_download(db, media_root, vid, i + 1)
    monkeypatch.setattr(worker.ytdlp, "extract_audio", _fake_extract_audio)

    assert backfill_audio(db, SimpleNamespace(ytdlp_sleep_interval=0)) == 1
    assert db.get(Download, ids[2]).audio_path is not None  # newest only
    assert db.get(Download, ids[0]).audio_path is None


# --- podcast audio backfill ----------------------------------------------

def _fake_extract_audio(video_id, output_path, *, sleep_interval=5, timeout=3600):
    """Stand-in for ytdlp.extract_audio: writes a .m4a next to output_path.

    Creates the parent dir first, mirroring the real extractor — an audio-only
    fetch has no prior video download to create the season folder.
    """
    p = Path(str(output_path)).with_suffix(".m4a")
    p.parent.mkdir(parents=True, exist_ok=True)
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


# --- audio-only: podcast without keeping the video -----------------------

def test_podcast_only_retains_audio_not_video(db):
    # Archive off, podcasts on: the videos are wanted as audio only.
    add_user(db, 1, download_enabled=False, generate_podcast=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)

    assert user_retained_video_ids(db) == {}       # nothing kept as video
    assert retained_video_ids(db) == set()
    assert user_retained_podcast_ids(db) == {1: set(ids)}
    assert retained_podcast_ids(db) == set(ids)


def test_enqueue_includes_podcast_only_videos(db):
    add_user(db, 1, download_enabled=False, generate_podcast=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 2)

    assert enqueue_pending(db) == 2  # audio-only channel still enqueues
    assert {d.video_id for d in db.query(Download).all()} == set(ids)


def test_worker_audio_only_fetches_audio_and_skips_video(db, tmp_path, monkeypatch):
    add_user(db, 1, download_enabled=False, generate_podcast=True)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    row = Download(video_id=ids[0], status="downloading")
    db.add(row)
    db.commit()

    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        media_root=str(tmp_path), ytdlp_format="best",
        ytdlp_sleep_interval=0, ytdlp_max_retries=1,
    ))
    monkeypatch.setattr(worker.ytdlp, "probe",
                        lambda vid: SimpleNamespace(duration_seconds=120, title="T",
                                                    description="D"))
    monkeypatch.setattr(worker, "classify_videos", lambda video_ids, db: {})

    def _no_download(*a, **k):
        raise AssertionError("audio-only must not download the full video")
    monkeypatch.setattr(worker.ytdlp, "download", _no_download)
    monkeypatch.setattr(worker.ytdlp, "extract_audio", _fake_extract_audio)
    # classify_videos probes YouTube over the network; stub it so the test
    # exercises the duration check and nothing else.
    monkeypatch.setattr(worker, "classify_videos", lambda video_ids, db: {})

    worker.process(row, db)

    assert row.status == "complete"
    assert row.file_path is None                  # no video file
    assert row.audio_path and row.audio_path.endswith(".m4a")
    # No Jellyfin NFO for an audio-only fetch.
    canon = naming.canonical_episode_dir(
        str(tmp_path), CHAN_TITLE, db.get(Video, ids[0]).published_at
    )
    assert not list(canon.glob("*.nfo"))


def _seed_download_with_audio(db, media, vid, epnum):
    """A completed download that has both a video file and extracted audio."""
    mkv = complete_download(db, media, vid, epnum)
    audio = mkv.with_suffix(".m4a")
    audio.write_bytes(b"audio-bytes")
    d = db.get(Download, vid)
    d.audio_path = str(audio)
    d.audio_size_bytes = audio.stat().st_size
    db.commit()
    return mkv, audio


def test_prune_drops_video_keeps_audio_when_podcast_only(db, tmp_path):
    media = str(tmp_path)
    add_user(db, 1, download_enabled=False, generate_podcast=True, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    mkv, audio = _seed_download_with_audio(db, media, ids[0], 1)

    assert run_prune(db) == 0                 # row survives — audio still wanted
    assert not mkv.exists()                   # video pruned
    assert not mkv.with_suffix(".nfo").exists()
    assert audio.exists()                     # audio kept
    d = db.get(Download, ids[0])
    assert d.file_path is None and d.audio_path == str(audio)


def test_prune_drops_audio_keeps_video_when_not_podcast(db, tmp_path):
    media = str(tmp_path)
    add_user(db, 1, download_enabled=True, generate_podcast=False, keep_last_n=0)
    add_sub(db, 1)
    ids = add_videos(db, 1)
    mkv, audio = _seed_download_with_audio(db, media, ids[0], 1)

    assert run_prune(db) == 0                 # row survives — video still wanted
    assert mkv.exists()                       # video kept
    assert not audio.exists()                 # audio pruned
    d = db.get(Download, ids[0])
    assert d.audio_path is None and d.file_path == str(mkv)
