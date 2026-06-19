"""End-to-end feed-proxy tests against in-memory SQLite with a mocked
upstream feed. Requires neither Postgres, Keycloak, nor network."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.models import (
    Category,
    OpmlToken,
    Subscription,
    User,
    VideoLiveStatus,
    VideoShort,
)
from youtube_subs_opml.web.routes import feed

CID = "UCabcdefghijklmnopqrstuv"
TOKEN = "testtoken123"

SAMPLE_FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><yt:videoId>shortone111</yt:videoId><title>short</title></entry>
  <entry><yt:videoId>realvideo22</yt:videoId><title>real</title></entry>
</feed>"""


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only the tables the feed route reads — skips Channel's Postgres ARRAY col.
    Base.metadata.create_all(
        engine,
        tables=[
            t.__table__
            for t in (
                User, Subscription, Category, OpmlToken,
                VideoShort, VideoLiveStatus,
            )
        ],
    )
    TestingSession = sessionmaker(bind=engine)

    with TestingSession() as seed:
        user = User(
            id=1, oidc_sub="s", email="e", include_shorts=True, include_live=True
        )
        seed.add(user)
        seed.add(OpmlToken(user_id=1, token=TOKEN))
        seed.add(Subscription(user_id=1, channel_id=CID, include_shorts=None))
        seed.add(Category(id=1, user_id=1, name="Tech", slug="tech", include_shorts=False))
        seed.add(Category(
            id=2, user_id=1, name="Live", slug="live", include_live=False
        ))
        seed.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(feed.router)
    app.dependency_overrides[get_db] = override_db

    # Mock the upstream YouTube fetch.
    def fake_get(url, **kwargs):
        return httpx.Response(
            200, content=SAMPLE_FEED, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(feed.httpx, "get", fake_get)
    # Deterministic classification (no real probing).
    monkeypatch.setattr(
        feed, "classify_videos",
        lambda ids, db: {"shortone111": True, "realvideo22": False},
    )
    monkeypatch.setattr(
        feed, "classify_live",
        lambda ids, db: {"shortone111": "upcoming", "realvideo22": "none"},
    )

    return TestClient(app)


def test_passthrough_keeps_shorts(client):
    """User default includes Shorts -> all-feed passthrough keeps everything."""
    resp = client.get(f"/feed/{TOKEN}/{CID}.xml")
    assert resp.status_code == 200
    assert b"shortone111" in resp.content
    assert b"realvideo22" in resp.content


def test_category_excludes_shorts(client):
    """Category 'tech' sets include_shorts=False -> Shorts filtered."""
    resp = client.get(f"/feed/{TOKEN}/tech/{CID}.xml")
    assert resp.status_code == 200
    assert b"shortone111" not in resp.content
    assert b"realvideo22" in resp.content


def test_category_excludes_live(client):
    """Category 'live' sets include_live=False -> upcoming/live dropped, Shorts kept."""
    resp = client.get(f"/feed/{TOKEN}/live/{CID}.xml")
    assert resp.status_code == 200
    assert b"shortone111" not in resp.content  # 'upcoming' dropped
    assert b"realvideo22" in resp.content       # 'none' kept (Shorts not filtered here)


def test_unknown_token_404(client):
    assert client.get(f"/feed/wrongtoken/{CID}.xml").status_code == 404


def test_not_subscribed_404(client):
    assert client.get(f"/feed/{TOKEN}/UCnotsubscribed00000000.xml").status_code == 404


def test_unknown_category_404(client):
    assert client.get(f"/feed/{TOKEN}/nope/{CID}.xml").status_code == 404


def test_upstream_failure_502(client, monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(feed.httpx, "get", boom)
    assert client.get(f"/feed/{TOKEN}/{CID}.xml").status_code == 502
