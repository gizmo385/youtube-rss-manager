"""Subscription reconciliation in sync_account.

Regression coverage for a UniqueViolation on ``subscriptions_pkey``: the PK is
(user_id, channel_id), so a re-sync must match existing rows on that key rather
than on account_id. Otherwise reconnecting an account (new account row) or
syncing a channel that was added manually tries to INSERT a row that already
exists. Runs against in-memory SQLite; the YouTube API calls are monkeypatched.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml import youtube as yt
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import Channel, Subscription, User, YoutubeAccount
from youtube_subs_opml.web.services import sync as sync_mod
from youtube_subs_opml.web.services.crypto import encrypt_token

USER_ID = 1


@pytest.fixture(autouse=True)
def _local_settings(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(User(id=USER_ID, oidc_sub="s", email="e"))
        session.commit()
        yield session


def _account(db, account_id: int = 1) -> YoutubeAccount:
    acct = YoutubeAccount(
        id=account_id,
        user_id=USER_ID,
        channel_id="UCowneraccount00000000",
        refresh_token_encrypted=encrypt_token("refresh"),
    )
    db.add(acct)
    db.commit()
    return acct


def _patch_fetch(monkeypatch, channel_ids):
    subs = [yt.Subscription(channel_id=c, title=f"T{c}", description="") for c in channel_ids]
    monkeypatch.setattr(sync_mod, "fetch_subscriptions", lambda creds: subs)
    monkeypatch.setattr(sync_mod, "fetch_channel_topics", lambda creds, ids: {})


def test_resync_adopts_manual_and_stale_rows_without_pk_collision(db, monkeypatch):
    """A manually-added channel and a row from a *different* account are both
    adopted into the syncing account instead of colliding on the PK."""
    from youtube_subs_opml.web.config import get_settings

    acct = _account(db, account_id=2)  # the account doing the sync

    # Pre-existing rows the naive INSERT would collide with:
    db.add(Channel(channel_id="UCmanual0000000000000", title="Manual"))
    db.add(Channel(channel_id="UColdacct0000000000000", title="Old"))
    db.add(Channel(channel_id="UCgone00000000000000000", title="Gone"))
    db.commit()
    # manual add (account_id NULL), a row owned by some other account id (99),
    # and a stale row already owned by *this* account that is no longer fetched.
    db.add(Subscription(user_id=USER_ID, channel_id="UCmanual0000000000000", account_id=None))
    db.add(Subscription(user_id=USER_ID, channel_id="UColdacct0000000000000", account_id=99))
    db.add(Subscription(user_id=USER_ID, channel_id="UCgone00000000000000000", account_id=2))
    db.commit()

    _patch_fetch(monkeypatch, [
        "UCmanual0000000000000",   # was manual  -> adopt
        "UColdacct0000000000000",  # was acct 99 -> adopt
        "UCbrandnew00000000000",   # new         -> insert
    ])

    # Must not raise IntegrityError.
    count = sync_mod.sync_account(acct, db, get_settings())
    db.commit()

    assert count == 3
    subs = {s.channel_id: s for s in db.query(Subscription).all()}
    assert subs["UCmanual0000000000000"].account_id == 2   # adopted
    assert subs["UColdacct0000000000000"].account_id == 2  # adopted
    assert "UCbrandnew00000000000" in subs                 # inserted
    # The stale row this account owned but no longer sees is removed.
    assert "UCgone00000000000000000" not in subs


def test_resync_leaves_other_accounts_stale_rows_alone(db, monkeypatch):
    """Stale removal only touches rows this account owns."""
    from youtube_subs_opml.web.config import get_settings

    acct = _account(db, account_id=2)
    db.add(Channel(channel_id="UCotheracct0000000000", title="Other"))
    db.commit()
    # Owned by account 99, not in this account's fetch — must be kept.
    db.add(Subscription(user_id=USER_ID, channel_id="UCotheracct0000000000", account_id=99))
    db.commit()

    _patch_fetch(monkeypatch, ["UCbrandnew00000000000"])
    sync_mod.sync_account(acct, db, get_settings())
    db.commit()

    subs = {s.channel_id: s for s in db.query(Subscription).all()}
    assert subs["UCotheracct0000000000"].account_id == 99  # untouched
    assert "UCbrandnew00000000000" in subs
