"""Two-way sync between Watch-tab watched state and FreshRSS read state.

Each run, per user:

1. List the ids of every FreshRSS item in the Watch tab's window, and which of
   them are read — two id-only requests. FreshRSS can't say which read states
   changed since a given time, so the whole window is re-listed every time.
2. Fetch link and feed for items not seen before, and record which video each
   one is (or that it's none of ours). Only items from this app's feed proxy,
   for this user's token, count — a link to a video in some unrelated feed
   isn't that video's entry.
3. Per video, compare both sides against the state they agreed on last time
   (``FreshRssItem.synced_read``). Whichever side changed wins; if both did,
   read/watched wins. A video with no history yet becomes watched if either
   side has it watched/read.

A video can be several FreshRSS items — a channel in two categories is two
feeds — and they all follow the video.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from youtube_subs_opml import nebula

from ..models import DownloadLink, FreshRssAccount, FreshRssItem, OpmlToken, WatchedVideo
from .crypto import decrypt_token
from .freshrss import READ, READING_LIST, FreshRssClient
from .watch import WINDOW, stage_watched

logger = logging.getLogger(__name__)

# The scheduled sync and the one triggered by a watched change can land at
# once; one user's sync must not interleave with itself.
_user_locks: defaultdict[int, threading.Lock] = defaultdict(threading.Lock)

# routes/feed.py: /feed/{token}/{channel_id}.xml or /feed/{token}/{slug}/{channel_id}.xml
_FEED_PATH = re.compile(r"/feed/(?P<token>[^/]+)/(?:[^/]+/)?[^/]+\.xml$")


@dataclass
class SyncResult:
    marked_read: int = 0
    marked_unread: int = 0
    marked_watched: int = 0
    marked_unwatched: int = 0


def is_our_feed(url: str, token: str) -> bool:
    m = _FEED_PATH.search(urlsplit(url).path)
    return bool(m and m.group("token") == token)


def video_key_for_link(link: str, jellyfin_items: dict[str, str]) -> str | None:
    """The Watch-tab key for an entry link: a YouTube video id, ``nebula:{slug}``,
    or — for a link the feed proxy rewrote to Jellyfin — the archived video's id
    via ``jellyfin_items`` (item id -> video id)."""
    u = urlsplit(link)
    host = (u.hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if u.path == "/watch":
            return (parse_qs(u.query).get("v") or [None])[0]
        parts = u.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] in ("shorts", "live"):
            return parts[1]
        return None
    if host == "youtu.be":
        return u.path.strip("/") or None
    if host == "nebula.tv" or host.endswith(".nebula.tv"):
        # Same slug rule as the Watch tab's Nebula items.
        if "/videos/" in u.path:
            return f"{nebula.ID_PREFIX}{u.path.rstrip('/').rsplit('/', 1)[-1]}"
        return None
    if "details?" in u.fragment:
        item_id = (parse_qs(u.fragment.split("?", 1)[1]).get("id") or [None])[0]
        return jellyfin_items.get(item_id) if item_id else None
    return None


def _record_new_items(db: Session, user_id: int, client: FreshRssClient, new_ids: list[int]) -> None:
    """Map ``new_ids`` to videos and store them, so they're never fetched again."""
    token = db.execute(select(OpmlToken.token).where(OpmlToken.user_id == user_id)).scalar_one_or_none()
    ours = {sid for sid, url in client.subscriptions().items() if is_our_feed(url, token)} if token else set()
    if not ours:
        # None of this FreshRSS's feeds are ours, so none of these items are
        # videos — no need to fetch them to find that out.
        db.add_all(FreshRssItem(user_id=user_id, item_id=i) for i in new_ids)
        db.commit()
        return

    jellyfin_items = {
        item: vid
        for vid, item in db.execute(
            select(DownloadLink.video_id, DownloadLink.jellyfin_item_id).where(
                DownloadLink.user_id == user_id, DownloadLink.jellyfin_item_id.is_not(None)
            )
        ).all()
        if item
    }
    for item in client.items(new_ids):
        key = video_key_for_link(item.link, jellyfin_items) if item.stream_id in ours else None
        db.add(FreshRssItem(user_id=user_id, item_id=item.id, video_key=key))
    db.commit()


def _load_items(db: Session, user_id: int) -> dict[int, FreshRssItem]:
    rows = db.execute(select(FreshRssItem).where(FreshRssItem.user_id == user_id)).scalars().all()
    return {row.item_id: row for row in rows}


def _target(app: bool, rows: list[FreshRssItem], read: set[int]) -> bool:
    """The state a video and its FreshRSS items should all end up in."""
    base = next((r.synced_read for r in rows if r.synced_read is not None), None)
    candidates: list[bool] = []
    if base is None or app != base:
        candidates.append(app)
    for r in rows:
        is_read = r.item_id in read
        if r.synced_read is None:
            # A new item only counts when it's read: an unread entry that just
            # arrived isn't a request to unwatch.
            if is_read:
                candidates.append(True)
        elif is_read != r.synced_read:
            candidates.append(is_read)
    if True in candidates:
        return True
    return False if candidates else bool(base)


def sync_user(db: Session, user_id: int, client: FreshRssClient, now: datetime | None = None) -> SyncResult:
    with _user_locks[user_id]:
        return _sync_user(db, user_id, client, now or datetime.now(UTC))


def _sync_user(db: Session, user_id: int, client: FreshRssClient, now: datetime) -> SyncResult:
    since = now - WINDOW
    window = set(client.item_ids(READING_LIST, since))
    read = set(client.item_ids(READ, since))

    known = _load_items(db, user_id)
    # Forget items that have aged out of the window (or been purged from FreshRSS).
    cutoff = int(since.timestamp() * 1_000_000)
    stale = [i for i in known if i < cutoff and i not in window]
    for n in range(0, len(stale), 500):
        db.execute(
            delete(FreshRssItem).where(FreshRssItem.user_id == user_id, FreshRssItem.item_id.in_(stale[n : n + 500]))
        )
    new_ids = sorted(i for i in window if i not in known)
    if new_ids:
        _record_new_items(db, user_id, client, new_ids)
        known = _load_items(db, user_id)

    by_key: dict[str, list[FreshRssItem]] = defaultdict(list)
    for item_id in window:
        row = known.get(item_id)
        if row is not None and row.video_key:
            by_key[row.video_key].append(row)

    watched = set()
    if by_key:
        watched = set(
            db.execute(
                select(WatchedVideo.video_key).where(
                    WatchedVideo.user_id == user_id, WatchedVideo.video_key.in_(list(by_key))
                )
            )
            .scalars()
            .all()
        )

    to_read: list[int] = []
    to_unread: list[int] = []
    to_watch: list[str] = []
    to_unwatch: list[str] = []
    for key, rows in by_key.items():
        app = key in watched
        target = _target(app, rows, read)
        if target != app:
            (to_watch if target else to_unwatch).append(key)
        for r in rows:
            if (r.item_id in read) != target:
                (to_read if target else to_unread).append(r.item_id)
            r.synced_read = target

    # FreshRSS first: if it fails, nothing here is committed and the next run
    # starts over from the same agreed state.
    client.set_read(to_read, True)
    client.set_read(to_unread, False)
    stage_watched(db, user_id, to_watch, True)
    stage_watched(db, user_id, to_unwatch, False)
    _touch(db, user_id, now)

    result = SyncResult(len(to_read), len(to_unread), len(to_watch), len(to_unwatch))
    if any((to_read, to_unread, to_watch, to_unwatch)):
        logger.info("FreshRSS sync for user %s: %s", user_id, result)
    return result


def _touch(db: Session, user_id: int, now: datetime) -> None:
    account = db.execute(select(FreshRssAccount).where(FreshRssAccount.user_id == user_id)).scalar_one_or_none()
    if account is not None:
        account.last_synced_at = now
    db.commit()


def client_for(account: FreshRssAccount) -> FreshRssClient:
    return FreshRssClient(
        base_url=account.base_url,
        username=account.username,
        api_password=decrypt_token(account.api_password_encrypted),
    )


def sync_all(db: Session) -> None:
    """Sync every user who has connected FreshRSS."""
    for account in db.execute(select(FreshRssAccount)).scalars().all():
        try:
            sync_user(db, account.user_id, client_for(account))
        except Exception:
            logger.exception("FreshRSS sync failed for user %s", account.user_id)
            db.rollback()


def sync_one(db: Session, user_id: int) -> None:
    """Sync just ``user_id``, if they've connected FreshRSS."""
    account = db.execute(select(FreshRssAccount).where(FreshRssAccount.user_id == user_id)).scalar_one_or_none()
    if account is not None:
        sync_user(db, user_id, client_for(account))
