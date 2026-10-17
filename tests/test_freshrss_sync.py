"""FreshRSS watched/read sync: link mapping, the three-way reconcile, and the
Greader API client's request shapes (against a mock transport)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    DownloadLink,
    FreshRssAccount,
    FreshRssItem,
    OpmlToken,
    User,
    Video,
    WatchedVideo,
)
from youtube_subs_opml.web.services import freshrss, freshrss_sync
from youtube_subs_opml.web.services.freshrss import READ, READING_LIST, FreshRssClient, FreshRssError, Item
from youtube_subs_opml.web.services.freshrss_sync import is_our_feed, sync_user, video_key_for_link

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
USER = 1
TOKEN = "tok123"
OURS = f"https://youtube-rss.example/feed/{TOKEN}/UCa.xml"
OURS_CAT = f"https://youtube-rss.example/feed/{TOKEN}/science/UCa.xml"


def _id(hours_ago: float) -> int:
    """A FreshRSS item id: crawl time in microseconds."""
    return int((NOW - timedelta(hours=hours_ago)).timestamp() * 1_000_000)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(User(id=USER, oidc_sub="s", email="e"))
    session.commit()
    session.add(OpmlToken(user_id=USER, token=TOKEN))
    session.add(FreshRssAccount(user_id=USER, base_url="https://rss.example", username="u", api_password_encrypted=b""))
    session.commit()
    try:
        yield session
    finally:
        session.close()


class FakeFreshRss:
    """In-memory stand-in for FreshRssClient."""

    def __init__(self):
        self.feeds = {"feed/1": OURS, "feed/2": OURS_CAT, "feed/9": "https://blog.example/rss"}
        self.entries: dict[int, Item] = {}
        self.read: set[int] = set()
        self.fetched: list[int] = []
        self.fail_writes = False

    def add(self, item_id: int, link: str, stream: str = "feed/1", read: bool = False) -> int:
        self.entries[item_id] = Item(id=item_id, stream_id=stream, link=link)
        if read:
            self.read.add(item_id)
        return item_id

    def subscriptions(self):
        return dict(self.feeds)

    def item_ids(self, stream, since):
        ids = [i for i in self.entries if i >= int(since.timestamp() * 1_000_000)]
        return [i for i in ids if i in self.read] if stream == READ else ids

    def items(self, ids):
        self.fetched.extend(ids)
        return [self.entries[i] for i in ids if i in self.entries]

    def set_read(self, ids, read):
        if ids and self.fail_writes:
            raise FreshRssError("boom")
        (self.read.update if read else self.read.difference_update)(ids)


def _watched(db: Session) -> set[str]:
    return set(db.execute(select(WatchedVideo.video_key).where(WatchedVideo.user_id == USER)).scalars().all())


def _watch(db: Session, *keys: str) -> None:
    db.add_all(WatchedVideo(user_id=USER, video_key=k) for k in keys)
    db.commit()


def _sync(db, fr, now=NOW):
    return sync_user(db, USER, fr, now=now)


# --- mapping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "key"),
    [
        ("https://www.youtube.com/watch?v=abc123", "abc123"),
        ("https://youtube.com/watch?v=abc123&t=10", "abc123"),
        ("https://www.youtube.com/shorts/sh0rt", "sh0rt"),
        ("https://youtu.be/xyz", "xyz"),
        ("https://nebula.tv/videos/tomscott-recent/", "nebula:tomscott-recent"),
        ("https://jf.example/web/#/details?id=jfitem1", "vid_archived"),
        ("https://jf.example/web/#/details?id=unknown", None),
        ("https://www.youtube.com/channel/UCa", None),
        ("https://blog.example/post", None),
    ],
)
def test_video_key_for_link(link, key):
    assert video_key_for_link(link, {"jfitem1": "vid_archived"}) == key


def test_is_our_feed_needs_this_users_token():
    assert is_our_feed(OURS, TOKEN)
    assert is_our_feed(OURS_CAT, TOKEN)
    assert not is_our_feed("https://youtube-rss.example/feed/other/UCa.xml", TOKEN)
    assert not is_our_feed("https://www.youtube.com/feeds/videos.xml?channel_id=UCa", TOKEN)


# --- reconcile -------------------------------------------------------------


def test_first_sync_takes_the_union(db):
    fr = FakeFreshRss()
    a = fr.add(_id(1), "https://www.youtube.com/watch?v=watched_here")
    b = fr.add(_id(2), "https://www.youtube.com/watch?v=read_there", read=True)
    c = fr.add(_id(3), "https://www.youtube.com/watch?v=neither")
    _watch(db, "watched_here")

    result = _sync(db, fr)

    assert fr.read == {a, b}
    assert _watched(db) == {"watched_here", "read_there"}
    assert (result.marked_read, result.marked_watched) == (1, 1)
    assert c not in fr.read


def test_items_from_other_feeds_are_ignored(db):
    """A blog post linking to a video isn't that video's entry."""
    fr = FakeFreshRss()
    fr.add(_id(1), "https://www.youtube.com/watch?v=v1", stream="feed/9", read=True)
    fr.add(_id(2), "https://www.youtube.com/watch?v=v2", stream="feed/9")
    _watch(db, "v2")

    _sync(db, fr)

    assert _watched(db) == {"v2"}
    assert fr.read == {_id(1)}


def test_items_are_fetched_once(db):
    fr = FakeFreshRss()
    fr.add(_id(1), "https://www.youtube.com/watch?v=v1")
    fr.add(_id(2), "https://blog.example/post", stream="feed/9")
    _sync(db, fr)
    _sync(db, fr)
    assert sorted(fr.fetched) == sorted([_id(1), _id(2)])


def test_no_token_means_nothing_is_ours_and_nothing_is_fetched(db):
    db.query(OpmlToken).delete()
    db.commit()
    fr = FakeFreshRss()
    fr.add(_id(1), "https://www.youtube.com/watch?v=v1", read=True)
    _sync(db, fr)
    assert fr.fetched == []
    assert _watched(db) == set()


def test_read_in_freshrss_marks_watched_and_unread_unwatches(db):
    fr = FakeFreshRss()
    i = fr.add(_id(1), "https://www.youtube.com/watch?v=v1")
    _sync(db, fr)
    assert _watched(db) == set()

    fr.read.add(i)
    _sync(db, fr)
    assert _watched(db) == {"v1"}

    fr.read.discard(i)
    _sync(db, fr)
    assert _watched(db) == set()


def test_watched_here_marks_read_and_unwatched_marks_unread(db):
    fr = FakeFreshRss()
    i = fr.add(_id(1), "https://www.youtube.com/watch?v=v1")
    _sync(db, fr)

    _watch(db, "v1")
    _sync(db, fr)
    assert i in fr.read

    db.query(WatchedVideo).delete()
    db.commit()
    _sync(db, fr)
    assert i not in fr.read
    assert _watched(db) == set()


def test_a_video_in_two_feeds_moves_as_one(db):
    fr = FakeFreshRss()
    a = fr.add(_id(1), "https://www.youtube.com/watch?v=v1", stream="feed/1")
    b = fr.add(_id(2), "https://www.youtube.com/watch?v=v1", stream="feed/2", read=True)
    _sync(db, fr)  # agreed: watched, both items read
    assert fr.read == {a, b} and _watched(db) == {"v1"}

    # One item marked unread in FreshRSS: the video and its other item follow.
    fr.read.discard(a)
    _sync(db, fr)
    assert fr.read == set() and _watched(db) == set()

    # And reading either one marks the whole video read again.
    fr.read.add(b)
    _sync(db, fr)
    assert fr.read == {a, b} and _watched(db) == {"v1"}


def test_new_unread_item_for_a_watched_video_gets_marked_read(db):
    fr = FakeFreshRss()
    a = fr.add(_id(5), "https://www.youtube.com/watch?v=v1", read=True)
    _sync(db, fr)
    b = fr.add(_id(1), "https://www.youtube.com/watch?v=v1", stream="feed/2")
    _sync(db, fr)
    assert fr.read == {a, b}
    assert _watched(db) == {"v1"}


def test_jellyfin_rewritten_link_maps_to_the_video(db):
    db.add(Video(video_id="arch1", channel_id="UCa"))
    db.add(DownloadLink(user_id=USER, video_id="arch1", link_path="/x", jellyfin_item_id="jf9"))
    db.commit()
    fr = FakeFreshRss()
    fr.add(_id(1), "https://jf.example/web/#/details?id=jf9", read=True)
    _sync(db, fr)
    assert _watched(db) == {"arch1"}


def test_failed_write_changes_nothing_here(db):
    fr = FakeFreshRss()
    i = fr.add(_id(1), "https://www.youtube.com/watch?v=v1")
    _sync(db, fr)
    _watch(db, "v1")
    fr.fail_writes = True
    with pytest.raises(FreshRssError):
        _sync(db, fr)
    db.rollback()
    row = db.get(FreshRssItem, (USER, i))
    assert row.synced_read is False  # still the old agreement, so it retries

    fr.fail_writes = False
    _sync(db, fr)
    assert i in fr.read


def test_items_outside_the_window_are_pruned(db):
    fr = FakeFreshRss()
    old = fr.add(_id(24 * 29), "https://www.youtube.com/watch?v=v1")
    _sync(db, fr)
    assert db.get(FreshRssItem, (USER, old)) is not None
    _sync(db, fr, now=NOW + timedelta(days=2))
    assert db.get(FreshRssItem, (USER, old)) is None


def test_last_synced_at_is_recorded(db):
    _sync(db, FakeFreshRss())
    account = db.execute(select(FreshRssAccount)).scalar_one()
    assert account.last_synced_at is not None


# --- client ----------------------------------------------------------------


class _Server:
    """A mock of FreshRSS's greader.php, enough for the client's calls."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/greader.php")
        if path == "/accounts/ClientLogin":
            form = parse_qs(request.content.decode())
            if form == {"Email": ["u"], "Passwd": ["pw"]}:
                return httpx.Response(200, text="SID=u/abc\nLSID=null\nAuth=u/abc\n")
            return httpx.Response(401, text="Unauthorized!")
        if request.headers.get("Authorization") != "GoogleLogin auth=u/abc":
            return httpx.Response(401)
        if path == "/reader/api/0/stream/items/ids":
            if request.url.params.get("c") == "2":
                return httpx.Response(200, json={"itemRefs": [{"id": "3"}]})
            return httpx.Response(200, json={"itemRefs": [{"id": "1"}, {"id": "2"}], "continuation": "2"})
        if path == "/reader/api/0/stream/items/contents":
            ids = parse_qs(request.content.decode())["i"]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": f"tag:google.com,2005:reader/item/{int(i):016x}",
                            "alternate": [{"href": f"https://www.youtube.com/watch?v=v{i}", "type": "text/html"}],
                            "origin": {"streamId": "feed/1"},
                        }
                        for i in ids
                    ]
                },
            )
        if path == "/reader/api/0/token":
            return httpx.Response(200, text="TOKEN\n")
        if path == "/reader/api/0/edit-tag":
            return httpx.Response(200, text="OK")
        if path == "/reader/api/0/user-info":
            return httpx.Response(200, json={"userName": "u"})
        return httpx.Response(404)


@pytest.fixture
def server(monkeypatch) -> _Server:
    srv = _Server()
    client = httpx.Client(transport=httpx.MockTransport(srv))

    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        return client.request(method, url, **kwargs)

    monkeypatch.setattr(freshrss.httpx, "request", request)
    monkeypatch.setattr(freshrss.httpx, "post", lambda url, **kw: request("POST", url, **kw))
    return srv


def test_client_verify(server):
    assert FreshRssClient("https://rss.example", "u", "pw").verify()
    assert not FreshRssClient("https://rss.example", "u", "wrong").verify()


def test_client_accepts_the_api_url_too(server):
    assert FreshRssClient("https://rss.example/api/greader.php/", "u", "pw").verify()


def test_client_pages_item_ids(server):
    ids = FreshRssClient("https://rss.example", "u", "pw").item_ids(READING_LIST, NOW)
    assert ids == [1, 2, 3]
    first = server.requests[1]
    assert first.url.params["s"] == READING_LIST
    assert first.url.params["ot"] == str(int(NOW.timestamp()))


def test_client_items_parse_hex_ids(server):
    items = FreshRssClient("https://rss.example", "u", "pw").items([1730000000000000])
    assert items == [
        Item(id=1730000000000000, stream_id="feed/1", link="https://www.youtube.com/watch?v=v1730000000000000")
    ]


def test_client_set_read_sends_repeated_ids_and_token(server):
    FreshRssClient("https://rss.example", "u", "pw").set_read([5, 6], False)
    edit = server.requests[-1]
    assert parse_qs(edit.content.decode()) == {"T": ["TOKEN"], "r": [READ], "i": ["5", "6"]}


# --- wiring ----------------------------------------------------------------


def test_sync_all_skips_a_failing_user(db, monkeypatch):
    calls = []

    def fake_sync(db_, user_id, client, now=None):
        calls.append(user_id)
        raise FreshRssError("down")

    monkeypatch.setattr(freshrss_sync, "sync_user", fake_sync)
    monkeypatch.setattr(freshrss_sync, "decrypt_token", lambda b: "pw")
    freshrss_sync.sync_all(db)
    assert calls == [USER]
