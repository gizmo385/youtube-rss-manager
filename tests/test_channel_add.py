"""Manual channel adds: API lookup when possible, public resolver as fallback."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import Channel, Subscription, User, YoutubeAccount
from youtube_subs_opml.web.routes import channels
from youtube_subs_opml.web.services import scheduler
from youtube_subs_opml.youtube import ChannelLookupError, ResolvedChannel

API_RESULT = ResolvedChannel(
    channel_id="UCnew", title="From API", description="api desc", topics=["Science"]
)
PUBLIC_RESULT = ResolvedChannel(
    channel_id="UCnew", title="From RSS", description="", topics=None
)


@pytest.fixture
def calls():
    """Records which resolver each request reached."""
    return []


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    with TestingSession() as s:
        s.add(User(id=1, oidc_sub="s1", email="e1"))
        s.commit()
    return TestingSession


@pytest.fixture
def client(monkeypatch, calls, session_factory):
    monkeypatch.setattr(
        channels, "get_settings", lambda: SimpleNamespace(base_url="http://test")
    )
    # Credentials and the poll nudge are irrelevant to what's under test.
    monkeypatch.setattr(channels, "decrypt_token", lambda blob: "refresh-token")
    monkeypatch.setattr(channels, "build_google_credentials", lambda tok, s: object())
    monkeypatch.setattr(scheduler, "trigger_poll_soon", lambda: None)

    def public(value):
        calls.append("public")
        return PUBLIC_RESULT

    monkeypatch.setattr(channels, "resolve_channel_public", public)

    def odb():
        db = session_factory()
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
    return TestClient(app, raise_server_exceptions=False)


def connect_account(session_factory):
    with session_factory() as s:
        s.add(
            YoutubeAccount(
                id=1, user_id=1, channel_id="UCme", refresh_token_encrypted=b"x"
            )
        )
        s.commit()


def api_raising(calls, exc):
    def resolve(creds, value):
        calls.append("api")
        raise exc

    return resolve


def stored_channel(session_factory) -> Channel | None:
    with session_factory() as s:
        return s.get(Channel, "UCnew")


def test_add_without_account_uses_public_resolver(client, calls, session_factory):
    resp = client.post("/channels/add", data={"channel_input": "@veritasium"})

    assert resp.status_code == 200
    assert calls == ["public"]
    with session_factory() as s:
        sub = s.execute(Subscription.__table__.select()).one()
        assert sub.channel_id == "UCnew"
        # account_id NULL is what marks the subscription as manual.
        assert sub.account_id is None
    assert stored_channel(session_factory).title == "From RSS"


def test_add_with_account_prefers_the_api(client, calls, monkeypatch, session_factory):
    connect_account(session_factory)

    def resolve(creds, value):
        calls.append("api")
        return API_RESULT

    monkeypatch.setattr(channels, "resolve_channel", resolve)

    assert client.post("/channels/add", data={"channel_input": "@v"}).status_code == 200
    assert calls == ["api"]
    channel = stored_channel(session_factory)
    assert channel.title == "From API"
    # Description and topics are the reason to try the API at all.
    assert channel.description == "api desc"
    assert channel.youtube_topics == ["Science"]


@pytest.mark.parametrize(
    "exc",
    [
        RefreshError("revoked"),
        ChannelLookupError("No channel found for '@v'"),
        httpx.ConnectError("boom"),
        RuntimeError("quota exceeded"),
    ],
    ids=["revoked-token", "api-miss", "network", "other"],
)
def test_api_failure_falls_back_to_public(
    client, calls, monkeypatch, session_factory, exc
):
    connect_account(session_factory)
    monkeypatch.setattr(channels, "resolve_channel", api_raising(calls, exc))

    resp = client.post("/channels/add", data={"channel_input": "@v"})

    assert resp.status_code == 200
    assert calls == ["api", "public"]
    assert stored_channel(session_factory).title == "From RSS"


def test_fallback_keeps_existing_description_and_topics(
    client, calls, monkeypatch, session_factory
):
    # A channel someone else already added through the API path.
    with session_factory() as s:
        s.add(
            Channel(
                channel_id="UCnew",
                title="Old title",
                description="api desc",
                youtube_topics=["Science"],
            )
        )
        s.commit()
    connect_account(session_factory)
    monkeypatch.setattr(channels, "resolve_channel", api_raising(calls, RefreshError()))

    assert client.post("/channels/add", data={"channel_input": "@v"}).status_code == 200
    channel = stored_channel(session_factory)
    # The public resolver knows the title but not the rest; blanking metadata
    # it simply can't see would be a downgrade.
    assert channel.title == "From RSS"
    assert channel.description == "api desc"
    assert channel.youtube_topics == ["Science"]


def test_unresolvable_input_is_a_400(client, monkeypatch, session_factory):
    def public(value):
        raise ChannelLookupError(f"Could not interpret '{value}' as a channel reference")

    monkeypatch.setattr(channels, "resolve_channel_public", public)

    resp = client.post("/channels/add", data={"channel_input": "not a channel!!"})
    assert resp.status_code == 400
    assert "Could not interpret" in resp.json()["detail"]


def test_youtube_unreachable_is_a_400(client, monkeypatch):
    def public(value):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(channels, "resolve_channel_public", public)

    resp = client.post("/channels/add", data={"channel_input": "@v"})
    assert resp.status_code == 400
    assert "Could not reach YouTube" in resp.json()["detail"]


def test_empty_input_is_a_400(client, calls):
    resp = client.post("/channels/add", data={"channel_input": "   "})
    assert resp.status_code == 400
    assert calls == []
