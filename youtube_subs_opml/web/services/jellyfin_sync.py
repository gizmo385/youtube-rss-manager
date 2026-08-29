"""Jellyfin playlist sync — reconcile per-user libraries and playlists.

Runs periodically in the web process (it's HTTP + DB, no media I/O). For each
user with a Jellyfin account it:

1. refreshes the library (only when there are freshly-downloaded files whose
   item id we don't yet know), then
2. resolves item ids for that user's hardlinks by matching file paths, then
3. reconciles each category's playlist to the set of items the user retains.

The reconcile is declarative, like the hardlink reconcile in the downloader:
desired membership is derived from the DB, actual membership is read from
Jellyfin, and the difference is added/removed. This is what lets a pruned video
fall out of playlists automatically on a later pass — we never have to remember a
deleted row's item id, because removals are computed from what's actually in the
playlist versus what should be.

Item resolution is path-based: Jellyfin has no ProviderId query filter, and each
user's library is a small per-user subtree, so listing their episodes and
matching ``Path`` is both necessary and cheap.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Category,
    CategoryPlaylist,
    ChannelCategory,
    DownloadLink,
    JellyfinAccount,
    Video,
)
from .crypto import decrypt_token
from .jellyfin import JellyfinClient, JellyfinError, normalise_path

logger = logging.getLogger(__name__)


def resolve_item_ids(db: Session, user_id: int, jf_user_id: str, client) -> int:
    """Fill in ``jellyfin_item_id`` for the user's hardlinks by path match.

    Returns the number newly resolved. Links whose file Jellyfin hasn't indexed
    yet are simply left for the next pass.
    """
    unresolved = db.execute(
        select(DownloadLink).where(
            DownloadLink.user_id == user_id,
            DownloadLink.jellyfin_item_id.is_(None),
            DownloadLink.link_path.is_not(None),
        )
    ).scalars().all()
    if not unresolved:
        return 0

    paths = client.episode_paths(jf_user_id)
    resolved = 0
    for link in unresolved:
        item_id = paths.get(normalise_path(link.link_path))
        if item_id:
            link.jellyfin_item_id = item_id
            resolved += 1
    if resolved:
        db.commit()
        logger.info("Resolved %d Jellyfin item ids for user %s", resolved, user_id)
    return resolved


def reconcile_playlists(db: Session, user_id: int, jf_user_id: str, client) -> None:
    """Make each category's Jellyfin playlist match the items the user retains.

    Desired membership per category = the resolved item ids of the user's
    hardlinks whose channel is assigned to that category. Playlists are created
    lazily on first need and their ids cached in ``category_playlists``.
    """
    categories = db.execute(
        select(Category).where(Category.user_id == user_id)
    ).scalars().all()

    # channel -> categories it belongs to (for this user)
    chan_to_cats: dict[str, set[int]] = defaultdict(set)
    for assignment in db.execute(
        select(ChannelCategory).where(ChannelCategory.user_id == user_id)
    ).scalars().all():
        chan_to_cats[assignment.channel_id].add(assignment.category_id)

    # resolved links + the channel each video belongs to
    links = db.execute(
        select(DownloadLink).where(
            DownloadLink.user_id == user_id,
            DownloadLink.jellyfin_item_id.is_not(None),
        )
    ).scalars().all()
    if links:
        video_channel = dict(
            db.execute(
                select(Video.video_id, Video.channel_id).where(
                    Video.video_id.in_([link.video_id for link in links])
                )
            ).all()
        )
    else:
        video_channel = {}

    desired: dict[int, set[str]] = defaultdict(set)
    for link in links:
        channel_id = video_channel.get(link.video_id)
        if channel_id is None:
            continue
        for cat_id in chan_to_cats.get(channel_id, ()):
            desired[cat_id].add(link.jellyfin_item_id)

    playlists = {
        cp.category_id: cp
        for cp in db.execute(
            select(CategoryPlaylist).where(CategoryPlaylist.user_id == user_id)
        ).scalars().all()
    }

    for category in categories:
        want = desired.get(category.id, set())
        cp = playlists.get(category.id)
        if not want and cp is None:
            continue  # nothing archived in this category yet

        if cp is None:
            playlist_id = client.create_playlist(category.name, jf_user_id)
            cp = CategoryPlaylist(
                user_id=user_id, category_id=category.id, playlist_id=playlist_id
            )
            db.add(cp)
            actual: list = []
        else:
            playlist_id = cp.playlist_id
            actual = client.playlist_entries(playlist_id, jf_user_id)

        actual_ids = {entry.item_id for entry in actual}
        to_add = sorted(want - actual_ids)
        if to_add:
            client.add_to_playlist(playlist_id, to_add, jf_user_id)

        to_remove = [entry.entry_id for entry in actual if entry.item_id not in want]
        if to_remove:
            try:
                client.remove_from_playlist(playlist_id, to_remove, jf_user_id)
            except JellyfinError as exc:
                # DELETE-with-API-key only works on builds carrying
                # jellyfin/jellyfin#14154. Non-fatal: stale entries linger until
                # the server is updated, but nothing else breaks.
                logger.warning(
                    "Could not remove %d stale playlist entries for user %s "
                    "(Jellyfin build may predate API-key delete support): %s",
                    len(to_remove), user_id, exc,
                )

        cp.last_synced_at = func.now()

    db.commit()


def sync_user(
    db: Session, user_id: int, jf_user_id: str, client, *, do_refresh: bool = True
) -> None:
    """Refresh, resolve item ids, and reconcile playlists for one user."""
    if do_refresh:
        has_unresolved = db.execute(
            select(DownloadLink.video_id)
            .where(
                DownloadLink.user_id == user_id,
                DownloadLink.jellyfin_item_id.is_(None),
                DownloadLink.link_path.is_not(None),
            )
            .limit(1)
        ).first()
        if has_unresolved:
            # Files land before Jellyfin indexes them; this pass may resolve
            # nothing and the next one picks them up.
            try:
                client.refresh_library()
            except JellyfinError as exc:
                logger.warning("Library refresh failed for user %s: %s", user_id, exc)

    resolve_item_ids(db, user_id, jf_user_id, client)
    reconcile_playlists(db, user_id, jf_user_id, client)


def sync_all(db: Session) -> None:
    """Sync every user who has configured a Jellyfin account."""
    accounts = db.execute(select(JellyfinAccount)).scalars().all()
    for account in accounts:
        if not account.jellyfin_user_id:
            logger.info(
                "Jellyfin account for user %s has no user GUID set; skipping",
                account.user_id,
            )
            continue
        try:
            client = JellyfinClient(
                base_url=account.base_url,
                api_key=decrypt_token(account.api_key_encrypted),
            )
            sync_user(db, account.user_id, account.jellyfin_user_id, client)
        except Exception:
            logger.exception("Jellyfin sync failed for user %s", account.user_id)
            db.rollback()
