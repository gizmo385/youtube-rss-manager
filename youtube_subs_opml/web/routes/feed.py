from __future__ import annotations

from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from youtube_subs_opml.opml import FEED_URL

from ..db import get_db
from ..models import Category, OpmlToken, Subscription, User
from ..services.shorts import classify_videos, resolve_include_shorts

router = APIRouter(prefix="/feed", tags=["feed"])

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"
_MEDIA = "http://search.yahoo.com/mrss/"
_UPSTREAM_TIMEOUT = 15.0

# Preserve the conventional prefixes when we re-serialize the feed.
ET.register_namespace("", _ATOM)
ET.register_namespace("yt", _YT)
ET.register_namespace("media", _MEDIA)


def _filter_shorts(xml_bytes: bytes, db: Session) -> bytes:
    """Drop ``<entry>`` elements whose video is a Short, then re-serialize."""
    root = ET.fromstring(xml_bytes)
    entries = root.findall(f"{{{_ATOM}}}entry")

    ids = []
    for entry in entries:
        vid_el = entry.find(f"{{{_YT}}}videoId")
        if vid_el is not None and vid_el.text:
            ids.append(vid_el.text)

    verdicts = classify_videos(ids, db)

    for entry in entries:
        vid_el = entry.find(f"{{{_YT}}}videoId")
        vid = vid_el.text if vid_el is not None else None
        if vid and verdicts.get(vid):  # True == Short -> remove
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

    cat_pref: bool | None = None
    if slug is not None:
        category = db.execute(
            select(Category).where(
                Category.user_id == user_id,
                Category.slug == slug,
            )
        ).scalar_one_or_none()
        if category is None:
            raise HTTPException(status_code=404)
        cat_pref = category.include_shorts

    user = db.get(User, user_id)
    include_shorts = resolve_include_shorts(
        sub.include_shorts, cat_pref, user.include_shorts
    )

    try:
        upstream = httpx.get(
            FEED_URL.format(channel_id=channel_id),
            timeout=_UPSTREAM_TIMEOUT,
            follow_redirects=True,
        )
        upstream.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Upstream feed fetch failed")

    if include_shorts:
        # Cheap passthrough — no probing needed when Shorts are kept.
        return Response(content=upstream.content, media_type="application/xml")

    filtered = _filter_shorts(upstream.content, db)
    return Response(content=filtered, media_type="application/xml")


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
