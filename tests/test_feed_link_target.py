"""link_target rewriting in the feed proxy.

Exercises ``feed._filter_feed`` directly with an explicit ``now`` so the
``hold`` 48-hour fallback is deterministic, plus the Jellyfin deep-link helper.
"""
from __future__ import annotations

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import Download, DownloadLink, User, Video
from youtube_subs_opml.web.routes import feed

ATOM = "http://www.w3.org/2005/Atom"
YT = "http://www.youtube.com/xml/schemas/2015"
JF_BASE = "https://jf.example.com"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
RECENT = "2026-08-28T06:00:00+00:00"   # 6h before NOW
AGED = "2026-08-24T00:00:00+00:00"     # >48h before NOW
YT_LINK = "https://www.youtube.com/watch?v={vid}"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[t.__table__ for t in (User, Video, Download, DownloadLink)],
    )
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, oidc_sub="s", email="e"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def build_feed(entries: list[dict]) -> bytes:
    parts = [
        '<?xml version="1.0"?>',
        f'<feed xmlns="{ATOM}" xmlns:yt="{YT}">',
    ]
    for e in entries:
        vid = e["id"]
        parts.append(
            f"<entry><yt:videoId>{vid}</yt:videoId><title>{vid}</title>"
            f'<link rel="alternate" href="{YT_LINK.format(vid=vid)}"/>'
            f"<published>{e['published']}</published></entry>"
        )
    parts.append("</feed>")
    return "".join(parts).encode()


def links_by_id(xml: bytes) -> dict[str, str]:
    root = ET.fromstring(xml)
    out = {}
    for entry in root.findall(f"{{{ATOM}}}entry"):
        vid = entry.find(f"{{{YT}}}videoId").text
        link = entry.find(f"{{{ATOM}}}link")
        out[vid] = link.get("href")
    return out


def seed_download(db, vid, status, *, item_id=None):
    db.add(Video(video_id=vid, channel_id="chanA", title=vid))
    db.add(Download(video_id=vid, status=status, file_path="/c/" + vid))
    if item_id is not None:
        db.add(DownloadLink(user_id=1, video_id=vid, jellyfin_item_id=item_id))
    db.commit()


def _filter(db, xml, link_target, *, base=JF_BASE):
    return feed._filter_feed(
        xml, db, drop_shorts=False, drop_live=False,
        link_target=link_target, user_id=1, jellyfin_base=base, now=NOW,
    )


def test_deep_link_format():
    assert feed.jellyfin_deep_link(JF_BASE, "itemA") == (
        "https://jf.example.com/web/#/details?id=itemA"
    )
    # trailing slash on the base is tolerated
    assert feed.jellyfin_deep_link(JF_BASE + "/", "itemA").endswith("/web/#/details?id=itemA")


def test_when_ready_rewrites_only_ready(db):
    seed_download(db, "readyvid0001", "complete", item_id="itemA")
    seed_download(db, "pendingvid01", "pending")
    xml = build_feed([
        {"id": "readyvid0001", "published": RECENT},
        {"id": "pendingvid01", "published": RECENT},
    ])

    links = links_by_id(_filter(db, xml, "when_ready"))

    assert links["readyvid0001"] == feed.jellyfin_deep_link(JF_BASE, "itemA")
    assert links["pendingvid01"] == YT_LINK.format(vid="pendingvid01")  # untouched


def test_when_ready_no_jellyfin_base_is_noop(db):
    seed_download(db, "readyvid0001", "complete", item_id="itemA")
    xml = build_feed([{"id": "readyvid0001", "published": RECENT}])

    links = links_by_id(_filter(db, xml, "when_ready", base=None))

    assert links["readyvid0001"] == YT_LINK.format(vid="readyvid0001")


def test_hold_drops_unready_recent_keeps_ready(db):
    seed_download(db, "readyvid0001", "complete", item_id="itemA")
    seed_download(db, "pendingvid01", "pending")
    xml = build_feed([
        {"id": "readyvid0001", "published": RECENT},
        {"id": "pendingvid01", "published": RECENT},
    ])

    links = links_by_id(_filter(db, xml, "hold"))

    assert links["readyvid0001"] == feed.jellyfin_deep_link(JF_BASE, "itemA")
    assert "pendingvid01" not in links  # dropped: not ready and recent


def test_hold_fallback_publishes_aged_and_failed(db):
    seed_download(db, "oldpendvid01", "pending")   # aged out
    seed_download(db, "failedvid001", "failed")     # terminal
    xml = build_feed([
        {"id": "oldpendvid01", "published": AGED},
        {"id": "failedvid001", "published": RECENT},
    ])

    links = links_by_id(_filter(db, xml, "hold"))

    # Both published with their YouTube link rather than vanishing.
    assert links["oldpendvid01"] == YT_LINK.format(vid="oldpendvid01")
    assert links["failedvid001"] == YT_LINK.format(vid="failedvid001")


def test_youtube_target_leaves_everything(db):
    seed_download(db, "readyvid0001", "complete", item_id="itemA")
    xml = build_feed([{"id": "readyvid0001", "published": RECENT}])

    links = links_by_id(_filter(db, xml, "youtube"))

    assert links["readyvid0001"] == YT_LINK.format(vid="readyvid0001")
