"""Quick switcher channel index: scoping and ordering."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base, get_db
from youtube_subs_opml.web.deps import get_current_user
from youtube_subs_opml.web.models import Channel, Subscription, User
from youtube_subs_opml.web.routes import quickswitch


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(User(id=1, oidc_sub="s1", email="e1"))
        s.add(User(id=2, oidc_sub="s2", email="e2"))
        s.add(Channel(channel_id="UCa", title="Veritasium"))
        s.add(Channel(channel_id="UCb", title="kurzgesagt"))
        s.add(Channel(channel_id="UCc", title="Someone Else"))
        s.commit()
        s.add(Subscription(user_id=1, channel_id="UCa"))
        s.add(Subscription(user_id=1, channel_id="UCb"))
        s.add(Subscription(user_id=2, channel_id="UCc"))  # other user's
        s.commit()

    def odb():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    def ou(db: Session = Depends(get_db)):
        return db.get(User, 1)

    app = FastAPI()
    app.include_router(quickswitch.router)
    app.dependency_overrides[get_db] = odb
    app.dependency_overrides[get_current_user] = ou
    return TestClient(app)


def test_lists_own_channels_sorted_case_insensitively(client):
    resp = client.get("/quickswitch/channels.json")
    assert resp.status_code == 200
    data = resp.json()
    # Only this user's subscriptions, ordered by lower-cased title.
    assert data == [
        {"id": "UCb", "title": "kurzgesagt"},
        {"id": "UCa", "title": "Veritasium"},
    ]
    assert all(row["id"] != "UCc" for row in data)  # other user's channel excluded
