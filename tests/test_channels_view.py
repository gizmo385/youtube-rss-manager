"""Channels stage: overview landing, ignore grouping, and the ignore toggle."""
from __future__ import annotations

from types import SimpleNamespace

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
    Subscription,
    User,
)
from youtube_subs_opml.web.routes import channels


@pytest.fixture
def client(monkeypatch):
    # base_url is the only setting these routes touch; stub it so the test
    # doesn't need a full environment.
    monkeypatch.setattr(
        channels, "get_settings", lambda: SimpleNamespace(base_url="http://test")
    )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    with TestingSession() as s:
        s.add(User(id=1, oidc_sub="s1", email="e1"))
        s.add(Channel(channel_id="UCa", title="Alpha"))
        s.add(Channel(channel_id="UCb", title="Bravo"))
        s.add(Channel(channel_id="UCc", title="Charlie"))
        s.commit()
        s.add(Category(id=7, user_id=1, name="Science", slug="science"))
        s.commit()
        # Alpha: uncategorized. Bravo: in Science. Charlie: ignored, in Science.
        s.add(Subscription(user_id=1, channel_id="UCa"))
        s.add(Subscription(user_id=1, channel_id="UCb"))
        s.add(Subscription(user_id=1, channel_id="UCc", ignored=True))
        s.add(ChannelCategory(user_id=1, channel_id="UCb", category_id=7))
        s.add(ChannelCategory(user_id=1, channel_id="UCc", category_id=7))
        s.commit()

    def odb():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def ou(db: Session = Depends(get_db)):
        return db.get(User, 1)

    app = FastAPI()
    app.include_router(channels.router)
    app.dependency_overrides[get_db] = odb
    app.dependency_overrides[get_current_user] = ou
    return TestClient(app)


def test_landing_shows_overview_not_a_channel(client):
    html = client.get("/channels").text
    # The overview is the landing view; no channel detail is rendered, so
    # nothing on screen can edit a channel by accident.
    assert 'class="eyebrow">Library' in html
    assert "At a glance" in html
    assert 'class="eyebrow">Channel<' not in html
    # The Overview row carries the selection instead of the first channel.
    assert 'class="chan-row overview-row selected"' in html


def test_selected_channel_still_opens_its_detail(client):
    html = client.get("/channels?selected=UCa").text
    assert 'class="eyebrow">Channel<' in html
    assert "Ignore channel" in html


def test_detail_offers_a_separate_audio_retention_field(client):
    """Both keep rows PATCH their own field — the template used to infer the
    field name from the label, which two "Keep" rows would have collided on."""
    html = client.get("/channels?selected=UCa").text
    assert '"field": "keep_last_n"' in html
    assert '"field": "keep_last_n_audio"' in html
    # Blank audio window shows the video window it defaults to (user default 15).
    assert 'placeholder="15"' in html


def test_audio_retention_saves_per_subscription(client):
    resp = client.post(
        "/channels/archive-pref",
        data={"channel_ids": "UCa", "selected": "UCa", "return": "detail",
              "field": "keep_last_n_audio", "value": "30", "filter": "All"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'value="30"' in resp.text
    # And blanking it goes back to inheriting.
    resp = client.post(
        "/channels/archive-pref",
        data={"channel_ids": "UCa", "selected": "UCa", "return": "detail",
              "field": "keep_last_n_audio", "value": "", "filter": "All"},
        headers={"HX-Request": "true"},
    )
    assert 'value="30"' not in resp.text


def test_ignored_channels_group_at_the_bottom(client):
    html = client.get("/channels/list").text
    groups = [line for line in html.splitlines() if "group-name" in line]
    # Uncategorized first, categories next, Ignored pinned last.
    assert ">Uncategorized<" in groups[0]
    assert ">Science<" in groups[1]
    assert ">Ignored<" in groups[-1]
    assert len(groups) == 3
    # Charlie is ignored, so it leaves Science and appears only under Ignored.
    science = html.index(">Science<")
    ignored = html.index(">Ignored<")
    assert html.index("Charlie") > ignored
    assert "Charlie" not in html[science:ignored]


def test_ignore_toggle_moves_the_channel_and_reports_state(client):
    resp = client.post(
        "/channels/ignore",
        data={"channel_ids": "UCa", "selected": "UCa", "return": "detail",
              "ignored": "true", "filter": "All"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Stop ignoring" in resp.text
    # The OOB list refresh now files Alpha under Ignored, not Uncategorized.
    html = client.get("/channels/list").text
    assert ">Uncategorized<" not in html
    assert html.index("Alpha") > html.index(">Ignored<")


def test_blank_selected_keeps_the_overview_selected(client):
    """A bulk write from the list sends selected="" while the overview shows;
    the refreshed list must not select the written channel."""
    resp = client.post(
        "/channels/assign",
        data={"channel_ids": "UCa", "category_id": "7", "selected": "", "filter": "All"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert 'class="chan-row overview-row selected"' in resp.text
    assert 'data-channel-id="UCa"' in resp.text
    assert 'selected"\n       data-title="alpha"' not in resp.text
