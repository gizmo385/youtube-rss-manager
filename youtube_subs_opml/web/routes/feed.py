from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Category,
    Download,
    DownloadLink,
    JellyfinAccount,
    OpmlToken,
    Subscription,
    User,
)
from ..services.feed_cache import load_feed
from ..services.live import classify_live, resolve_include_live
from ..services.prefs import resolve
from ..services.shorts import classify_videos, resolve_include_shorts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feed", tags=["feed"])

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_MEDIA = "http://search.yahoo.com/mrss/"

# A download that never completes must not vanish forever under ``hold``: after
# this long we publish the entry with its original YouTube link instead.
_HOLD_FALLBACK = timedelta(hours=48)

# Preserve the conventional prefixes when we re-serialize the feed.
ET.register_namespace("", _ATOM)
ET.register_namespace("yt", _YT)
ET.register_namespace("media", _MEDIA)


def jellyfin_deep_link(base_url: str, item_id: str) -> str:
    """A web deep link to a Jellyfin item. Jellyfin resolves the item and
    redirects, so the server id can be omitted."""
    return f"{base_url.rstrip('/')}/web/#/details?id={item_id}"


def _rewrite_link(entry: ET.Element, href: str) -> None:
    """Point an entry's alternate ``<link>`` at ``href`` (Jellyfin, here)."""
    link = entry.find(f"{{{_ATOM}}}link")
    if link is not None:
        link.set("href", href)


def _entry_published(entry: ET.Element) -> datetime | None:
    el = entry.find(f"{{{_ATOM}}}published")
    if el is None or not el.text:
        return None
    try:
        dt = datetime.fromisoformat(el.text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _filter_feed(
    xml_bytes: bytes,
    db: Session,
    *,
    drop_shorts: bool,
    drop_live: bool,
    link_target: str = "youtube",
    user_id: int | None = None,
    jellyfin_base: str | None = None,
    now: datetime | None = None,
) -> bytes:
    """Drop unwanted ``<entry>`` elements and rewrite links, then re-serialize.

    Removes Shorts (when ``drop_shorts``) and currently upcoming/live videos
    (when ``drop_live``). Then applies ``link_target``:

    - ``youtube``    — leave the entry's YouTube link (default).
    - ``when_ready`` — rewrite the link to Jellyfin once the download is complete
      and this user's item id is known; otherwise leave the YouTube link, so the
      entry corrects itself on a later poll.
    - ``hold``       — drop the entry until it's ready in Jellyfin, *except* as a
      fallback: a failed/skipped download, or one older than ``_HOLD_FALLBACK``,
      is published with its YouTube link rather than disappearing silently.

    Classification and download state are fetched only for what's needed.
    """
    root = ET.fromstring(xml_bytes)
    entries = root.findall(f"{{{_ATOM}}}entry")

    ids = []
    for entry in entries:
        vid_el = entry.find(f"{{{_YT}}}videoId")
        if vid_el is not None and vid_el.text:
            ids.append(vid_el.text)

    shorts = classify_videos(ids, db) if drop_shorts else {}
    live = classify_live(ids, db) if drop_live else {}

    rewrite = link_target in ("when_ready", "hold") and jellyfin_base
    status_by_id: dict[str, str] = {}
    item_by_id: dict[str, str] = {}
    if rewrite and ids:
        status_by_id = dict(
            db.execute(
                select(Download.video_id, Download.status).where(
                    Download.video_id.in_(ids)
                )
            ).all()
        )
        item_by_id = {
            vid: item
            for vid, item in db.execute(
                select(DownloadLink.video_id, DownloadLink.jellyfin_item_id).where(
                    DownloadLink.user_id == user_id,
                    DownloadLink.video_id.in_(ids),
                )
            ).all()
            if item
        }
    now = now or datetime.now(timezone.utc)

    for entry in entries:
        vid_el = entry.find(f"{{{_YT}}}videoId")
        vid = vid_el.text if vid_el is not None else None
        if not vid:
            continue
        if drop_shorts and shorts.get(vid):  # True == Short
            root.remove(entry)
            continue
        if drop_live and live.get(vid) in ("upcoming", "live"):
            root.remove(entry)
            continue

        if not rewrite:
            continue

        item_id = item_by_id.get(vid)
        ready = status_by_id.get(vid) == "complete" and item_id
        if ready:
            _rewrite_link(entry, jellyfin_deep_link(jellyfin_base, item_id))
        elif link_target == "hold":
            published = _entry_published(entry)
            failed = status_by_id.get(vid) in ("failed", "skipped")
            aged_out = published is not None and now - published > _HOLD_FALLBACK
            if not (failed or aged_out):
                root.remove(entry)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _serve_feed(
    token: str,
    channel_id: str,
    slug: str | None,
    db: Session,
) -> Response:
    """Proxy a channel's YouTube RSS feed, filtering Shorts per the user's
    current preference (subscription > category > user).

    Every channel in generated OPML points here regardless of its Shorts
    setting, so the feed URL stays stable when the preference is toggled — only
    the filtering behavior below changes.
    """
    opml_token = db.execute(
        select(OpmlToken).where(OpmlToken.token == token)
    ).scalar_one_or_none()
    if opml_token is None:
        raise HTTPException(status_code=404)
    user_id = opml_token.user_id

    # Only proxy channels the user is actually subscribed to (no open proxy).
    sub = db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.channel_id == channel_id,
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    cat_shorts_pref: bool | None = None
    cat_live_pref: bool | None = None
    cat_link_pref: str | None = None
    if slug is not None:
        category = db.execute(
            select(Category).where(
                Category.user_id == user_id,
                Category.slug == slug,
            )
        ).scalar_one_or_none()
        if category is None:
            raise HTTPException(status_code=404)
        cat_shorts_pref = category.include_shorts
        cat_live_pref = category.include_live
        cat_link_pref = category.link_target

    user = db.get(User, user_id)
    include_shorts = resolve_include_shorts(
        sub.include_shorts, cat_shorts_pref, user.include_shorts
    )
    include_live = resolve_include_live(
        sub.include_live, cat_live_pref, user.include_live
    )
    drop_shorts = not include_shorts
    drop_live = not include_live

    link_target = resolve(sub.link_target, cat_link_pref, user.link_target)
    jellyfin_base: str | None = None
    if link_target in ("when_ready", "hold"):
        account = db.execute(
            select(JellyfinAccount).where(JellyfinAccount.user_id == user_id)
        ).scalar_one_or_none()
        jellyfin_base = account.base_url if account else None
        # Without somewhere to link, "when_ready"/"hold" can't ever resolve — fall
        # back to plain YouTube behavior rather than hiding every entry forever.
        if not jellyfin_base:
            link_target = "youtube"

    # Serve only from the cache the poller keeps warm — never fetch YouTube on
    # this request. A reader polls every feed URL at once; proxying each straight
    # to YouTube turned that into a burst the IP got soft-throttled for. The
    # poller is the sole fetcher (spaced out, backed off), so the fan-out here is
    # just cheap cache reads.
    #
    # A miss means the poller hasn't reached this channel yet (a fresh deploy
    # before the first sweep, or a just-added channel). Return a retryable 503
    # rather than fetching — fetching is exactly what caused the throttling. An
    # add/sync nudges a poll, and startup warms the cache, so misses are brief.
    content = load_feed(db, channel_id)
    if content is None:
        logger.info("Feed cache miss for %s (not polled yet); returning 503", channel_id)
        raise HTTPException(
            status_code=503,
            detail="Feed not ready yet; retry shortly.",
            headers={"Retry-After": "120"},
        )

    headers = {"X-Feed-Cache": "hit"}
    if not drop_shorts and not drop_live and link_target == "youtube":
        # Cheap passthrough — nothing to filter or rewrite.
        return Response(content=content, media_type="application/xml", headers=headers)

    filtered = _filter_feed(
        content,
        db,
        drop_shorts=drop_shorts,
        drop_live=drop_live,
        link_target=link_target,
        user_id=user_id,
        jellyfin_base=jellyfin_base,
    )
    return Response(content=filtered, media_type="application/xml", headers=headers)


@router.get("/{token}/{channel_id}.xml")
def feed_all(
    token: str,
    channel_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Channel feed for the all-subscriptions OPML (no category context)."""
    return _serve_feed(token, channel_id, None, db)


@router.get("/{token}/{slug}/{channel_id}.xml")
def feed_in_category(
    token: str,
    slug: str,
    channel_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Channel feed scoped to a category, so the category's Shorts preference
    participates in the cascade."""
    return _serve_feed(token, channel_id, slug, db)
