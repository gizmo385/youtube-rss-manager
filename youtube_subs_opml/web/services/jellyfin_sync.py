"""Jellyfin playlist sync — reconcile per-user libraries and playlists.

Runs periodically in the web process (it's HTTP + DB, no media I/O). For each
user with a Jellyfin account it:

1. refreshes the library (only when there are freshly-downloaded files whose
   item id we don't yet know), then
2. resolves item ids for that user's hardlinks by matching file paths, then
3. reconciles each category's playlist to the *ordered* list of items the user
   retains.

The reconcile is declarative, like the hardlink reconcile in the downloader:
desired membership is derived from the DB, actual membership is read from
Jellyfin, and the difference is added/removed. This is what lets a pruned video
fall out of playlists automatically on a later pass — we never have to remember a
deleted row's item id, because removals are computed from what's actually in the
playlist versus what should be.

Order is part of the desired state, not an afterthought. A playlist has no sort
field — clients play it in stored order — and adds always append, so a playlist
built from a set lands in whatever order the ids happened to iterate in, which
is item-GUID order: random. Episodes are therefore kept newest-upload-first,
matching how you'd scan a subscription feed. See ``_rewrite_playlist`` for why
fixing the order means rewriting the list rather than moving entries.

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


def _order_key(published_at, video_id: str) -> tuple:
    """Sort key placing the newest upload first and undated videos last.

    The video id breaks ties (two uploads sharing a timestamp) so the desired
    order is a pure function of the data — an order that wobbled between passes
    would make every sync think the playlist needed rewriting.
    """
    if published_at is None:
        return (1, 0.0, video_id)
    return (0, -published_at.timestamp(), video_id)


def _rewrite_playlist(
    client, playlist_id: str, jf_user_id: str, actual: list, want: list[str],
    user_id: int,
) -> None:
    """Make the playlist hold exactly ``want``, in that order.

    An API key has no way to reorder in place: ``/Items/{id}/Move`` and the bulk
    playlist update both authorise against the *calling user*, which an API key
    isn't, so they 403 — add (which only ever appends) and remove are the whole
    toolbox. Correcting the order therefore means emptying the playlist and
    re-adding in order, which is two requests and idempotent.

    When removal isn't available — a Jellyfin build predating
    jellyfin/jellyfin#14154 — this degrades to appending whatever is missing:
    membership still converges on the right episodes, only their order can't be
    fixed. The pre-removal contents are used as the baseline in that case, so a
    call that failed after removing some entries can't leave duplicates behind;
    the next pass sees the shortfall and adds them.
    """
    entry_ids = [entry.entry_id for entry in actual]
    current = [entry.item_id for entry in actual]

    if entry_ids:
        try:
            client.remove_from_playlist(playlist_id, entry_ids, jf_user_id)
            current = []
        except JellyfinError as exc:
            logger.warning(
                "Could not clear %d playlist entries for user %s, so its order "
                "can't be corrected (Jellyfin build may predate API-key delete "
                "support): %s",
                len(entry_ids), user_id, exc,
            )

    present = set(current)
    to_add = [item_id for item_id in want if item_id not in present]
    if to_add:
        client.add_to_playlist(playlist_id, to_add, jf_user_id)


def reconcile_playlists(db: Session, user_id: int, jf_user_id: str, client) -> None:
    """Make each category's Jellyfin playlist match the items the user retains.

    Desired contents per category = the resolved item ids of the user's
    hardlinks whose channel is assigned to that category, newest upload first.
    Playlists are created lazily on first need and their ids cached in
    ``category_playlists``.
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

    # resolved links + the channel and upload date of each video
    links = db.execute(
        select(DownloadLink).where(
            DownloadLink.user_id == user_id,
            DownloadLink.jellyfin_item_id.is_not(None),
        )
    ).scalars().all()
    if links:
        videos = {
            video_id: (channel_id, published_at)
            for video_id, channel_id, published_at in db.execute(
                select(Video.video_id, Video.channel_id, Video.published_at).where(
                    Video.video_id.in_([link.video_id for link in links])
                )
            ).all()
        }
    else:
        videos = {}

    # Newest upload first, undated last, video id breaking ties so the order is
    # stable across passes (an unstable order would rewrite the playlist on
    # every sync).
    ordered_links = sorted(
        (link for link in links if link.video_id in videos),
        key=lambda link: _order_key(videos[link.video_id][1], link.video_id),
    )

    desired: dict[int, list[str]] = defaultdict(list)
    for link in ordered_links:
        channel_id = videos[link.video_id][0]
        for cat_id in chan_to_cats.get(channel_id, ()):
            desired[cat_id].append(link.jellyfin_item_id)

    playlists = {
        cp.category_id: cp
        for cp in db.execute(
            select(CategoryPlaylist).where(CategoryPlaylist.user_id == user_id)
        ).scalars().all()
    }

    for category in categories:
        want = desired.get(category.id, [])
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

        # Compare the full sequence, not the set: a playlist holding the right
        # episodes in the wrong order is still wrong.
        if [entry.item_id for entry in actual] != want:
            _rewrite_playlist(client, playlist_id, jf_user_id, actual, want, user_id)

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
