"""SponsorBlock cuts of podcast audio: which are wanted, and the downloader
pass that makes, re-makes and removes them.

ffmpeg is faked: the pass is handed a ``cut`` that just writes a file, and a
``lookup`` in place of SponsorBlock.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.downloader import sponsor_cuts
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    AudioCut,
    Category,
    Channel,
    ChannelCategory,
    Download,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.services.archive import wanted_audio_cuts
from youtube_subs_opml.web.services.sponsorblock import Segment

CHAN = "UCchannel0000000000000"
VID = "vid00000001"
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture
def db(tmp_path) -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Channel(channel_id=CHAN, title="Chan"))
    session.add(
        User(
            id=1,
            oidc_sub="s1",
            email="e1",
            generate_podcast=True,
            podcast_sponsorblock="remove",
            podcast_sponsorblock_categories="sponsor",
        )
    )
    session.commit()
    session.add(Subscription(user_id=1, channel_id=CHAN))
    session.add(
        Video(video_id=VID, channel_id=CHAN, title="Ep", published_at=NOW - timedelta(hours=2), duration_seconds=600)
    )
    audio = tmp_path / "ep.m4a"
    audio.write_bytes(b"original")
    session.add(Download(video_id=VID, status="complete", audio_path=str(audio), audio_size_bytes=8))
    session.commit()
    try:
        yield session
    finally:
        session.close()


class Fakes:
    """Stands in for SponsorBlock and ffmpeg, recording what was asked of them."""

    def __init__(self, segments: list[Segment] | None):
        self.segments = segments
        self.lookups: list[str] = []
        self.cuts: list[tuple[Path, Path, list]] = []
        self.fail = False

    def lookup(self, video_id: str) -> list[Segment] | None:
        self.lookups.append(video_id)
        return self.segments

    def cut(self, src: Path, dst: Path, ranges: list) -> int | None:
        if self.fail:
            raise sponsor_cuts.CutError("boom")
        self.cuts.append((src, dst, ranges))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"cut")
        return 570


def refresh(db: Session, tmp_path: Path, fakes: Fakes, now: datetime = NOW) -> int:
    return sponsor_cuts.refresh_audio_cuts(db, str(tmp_path / "media"), now=now, lookup=fakes.lookup, cut=fakes.cut)


def cuts(db: Session) -> list[AudioCut]:
    db.expire_all()
    return list(db.query(AudioCut).all())


def path_of(cut: AudioCut) -> Path:
    assert cut.path is not None
    return Path(cut.path)


# --- planning --------------------------------------------------------------


def test_plan_keeps_chosen_kinds_merged_and_clipped():
    segs = [
        Segment(100, 130, "sponsor"),
        Segment(130.5, 140, "selfpromo"),  # near enough to the sponsor read to cut as one
        Segment(50, 60, "intro"),  # not chosen
        Segment(590, 640, "sponsor"),  # runs past the end
    ]
    assert sponsor_cuts.plan(segs, ["sponsor", "selfpromo"], 600) == [(100, 140), (590, 600)]
    assert sponsor_cuts.fingerprint([(100, 140), (590.04, 600)]) == "100.0-140.0,590.0-600.0"


def test_concat_list_keeps_the_parts_between_cuts():
    script = sponsor_cuts.concat_list(Path("/m/It's on.m4a"), [(0, 12.5), (100, 140)])
    assert script.splitlines() == [
        "ffconcat version 1.0",
        "file '/m/It'\\''s on.m4a'",
        "inpoint 12.500",
        "outpoint 100.000",
        "file '/m/It'\\''s on.m4a'",
        "inpoint 140.000",
    ]


# --- what's wanted ---------------------------------------------------------


def test_wanted_follows_the_podcast_cascade(db):
    assert wanted_audio_cuts(db) == {VID: {"sponsor"}}
    db.add(Category(id=3, user_id=1, name="C", slug="c", podcast_sponsorblock="off"))
    db.add(ChannelCategory(user_id=1, channel_id=CHAN, category_id=3))
    db.commit()
    assert wanted_audio_cuts(db) == {}


def test_wanted_one_cut_per_distinct_choice(db):
    db.add(
        User(
            id=2,
            oidc_sub="s2",
            email="e2",
            generate_podcast=True,
            podcast_sponsorblock="remove",
            podcast_sponsorblock_categories="sponsor,intro",
        )
    )
    db.add(User(id=3, oidc_sub="s3", email="e3", generate_podcast=True, podcast_sponsorblock="off"))
    db.commit()
    db.add_all([Subscription(user_id=2, channel_id=CHAN), Subscription(user_id=3, channel_id=CHAN)])
    db.commit()
    assert wanted_audio_cuts(db) == {VID: {"sponsor", "sponsor,intro"}}


def test_nothing_wanted_without_a_podcast(db):
    db.get_one(User, 1).generate_podcast = False
    db.commit()
    assert wanted_audio_cuts(db) == {}


# --- the pass --------------------------------------------------------------


def test_cuts_then_rechecks_only_when_due(db, tmp_path):
    fakes = Fakes([Segment(100, 130, "sponsor"), Segment(200, 210, "intro")])
    assert refresh(db, tmp_path, fakes) == 1
    [row] = cuts(db)
    assert (row.categories, row.segments, row.size_bytes, row.duration_seconds) == ("sponsor", "100.0-130.0", 3, 570)
    assert path_of(row).read_bytes() == b"cut"
    assert fakes.cuts[0][2] == [(100, 130)]

    # Checked a moment ago: left alone.
    assert refresh(db, tmp_path, fakes, NOW + timedelta(hours=1)) == 0
    assert len(fakes.lookups) == 1

    # Due again, nothing new: no re-cut.
    assert refresh(db, tmp_path, fakes, NOW + timedelta(hours=7)) == 0
    assert len(fakes.lookups) == 2 and len(fakes.cuts) == 1


def test_recut_when_segments_change_and_old_copy_goes(db, tmp_path):
    fakes = Fakes([Segment(100, 130, "sponsor")])
    refresh(db, tmp_path, fakes)
    old = path_of(cuts(db)[0])

    fakes.segments = [Segment(100, 130, "sponsor"), Segment(400, 460, "sponsor")]
    assert refresh(db, tmp_path, fakes, NOW + timedelta(hours=7)) == 1
    [row] = cuts(db)
    assert row.segments == "100.0-130.0,400.0-460.0"
    assert path_of(row) != old and not old.exists()


def test_no_segments_yet_serves_the_original_and_cuts_once_they_arrive(db, tmp_path):
    fakes = Fakes([])
    assert refresh(db, tmp_path, fakes) == 0
    [row] = cuts(db)
    assert (row.segments, row.path) == ("", None)

    fakes.segments = [Segment(30, 60, "sponsor")]
    assert refresh(db, tmp_path, fakes, NOW + timedelta(hours=7)) == 1
    assert cuts(db)[0].path is not None


def test_old_episodes_stop_being_rechecked(db, tmp_path):
    fakes = Fakes([])
    refresh(db, tmp_path, fakes)
    refresh(db, tmp_path, fakes, NOW + sponsor_cuts.RECHECK_FOR + timedelta(hours=1))
    assert len(fakes.lookups) == 1


def test_unreachable_sponsorblock_is_retried_next_pass(db, tmp_path):
    fakes = Fakes(None)
    refresh(db, tmp_path, fakes)
    assert cuts(db) == []
    refresh(db, tmp_path, fakes)
    assert len(fakes.lookups) == 2


def test_failed_cut_is_retried_even_on_an_old_episode(db, tmp_path):
    fakes = Fakes([Segment(100, 130, "sponsor")])
    fakes.fail = True
    assert refresh(db, tmp_path, fakes) == 1
    [row] = cuts(db)
    assert (row.segments, row.path) == (None, None)

    fakes.fail = False
    assert refresh(db, tmp_path, fakes, NOW + sponsor_cuts.RECHECK_FOR + timedelta(hours=1)) == 1
    assert cuts(db)[0].path is not None


def test_unwanted_cuts_are_removed(db, tmp_path):
    fakes = Fakes([Segment(100, 130, "sponsor")])
    refresh(db, tmp_path, fakes)
    path = path_of(cuts(db)[0])

    db.get_one(User, 1).podcast_sponsorblock = "off"
    db.commit()
    refresh(db, tmp_path, fakes)
    assert cuts(db) == [] and not path.exists()
