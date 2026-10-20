"""The Watch tab: recent uploads by category, with per-user watched state.

The page ships its whole feed as embedded JSON and the client does the
filtering, layout switching and keyboard navigation — those are all instant,
local operations on a few hundred items. Only watched-state changes round-trip.

Archived videos play in the page straight from the archive mount (``/media``),
rather than through Jellyfin: streaming from Jellyfin would put the account's
API key — an admin-level dashboard key — in the browser. The key stays
server-side and is only used to mark the item played (``/played``).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Download, DownloadLink, JellyfinAccount, Subscription, User, Video
from ..services import sponsorblock
from ..services.crypto import decrypt_token
from ..services.jellyfin import JellyfinClient, JellyfinError
from ..services.stats import shell_stats
from ..services.watch import build_watch_feed, save_progress, search_videos, set_watched
from ..templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watch", tags=["watch"])


class WatchedUpdate(BaseModel):
    # Bounded so "mark the whole category watched" fits but nothing silly does.
    keys: list[str] = Field(max_length=2000)
    watched: bool


class ProgressUpdate(BaseModel):
    key: str = Field(max_length=255)
    position: float = Field(ge=0)
    duration: float | None = Field(default=None, ge=0)


@router.get("")
def watch_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "watch.html",
        context={
            "user": user,
            "active_nav": "watch",
            "stats": shell_stats(user, db),
            "feed": build_watch_feed(user, db),
        },
    )


@router.get("/feed")
def watch_feed(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The same feed the page embeds, for its periodic refresh."""
    return build_watch_feed(user, db)


@router.get("/search")
def search(
    q: str = Query("", max_length=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Videos matching ``q`` across everything stored, beyond the feed's
    window and including descriptions — what the page shows under its own
    matches."""
    return {"query": q, "videos": search_videos(user, db, q)}


@router.post("/watched", status_code=204)
def update_watched(
    body: WatchedUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    set_watched(user, db, [k[:255] for k in body.keys], body.watched)
    # Push the change to FreshRSS (a no-op without an account there).
    from ..services.scheduler import sync_freshrss_soon

    sync_freshrss_soon(user.id)
    return Response(status_code=204)


@router.post("/progress", status_code=204)
def update_progress(
    body: ProgressUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Save the in-page player's position, for resuming and "Continue watching"."""
    save_progress(user, db, body.key, body.position, body.duration)
    return Response(status_code=204)


@router.get("/segments/{video_id}")
def video_segments(
    video_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The SponsorBlock mode for this video's channel and, unless it's off,
    the segments to show or skip, in seconds."""
    channel_id = db.execute(select(Video.channel_id).where(Video.video_id == video_id)).scalar_one_or_none()
    mode = sponsorblock.resolve_mode(db, user, channel_id) if channel_id else "off"
    if mode == "off":
        return {"mode": mode, "segments": []}
    found = sponsorblock.segments(video_id, sponsorblock.user_categories(user))
    return {
        "mode": mode,
        "segments": [{"start": s.start, "end": s.end, "category": s.category} for s in found],
    }


@router.get("/media/{video_id}")
def archived_media(
    video_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream an archived video for the in-page player, with range support
    for seeking. Only for channels the user subscribes to, so this isn't an
    open file server over the archive."""
    path = db.execute(
        select(Download.file_path)
        .join(Video, Video.video_id == Download.video_id)
        .join(Subscription, Subscription.channel_id == Video.channel_id)
        .where(
            Download.video_id == video_id,
            Download.status == "complete",
            Download.file_path.is_not(None),
            Subscription.user_id == user.id,
        )
    ).scalar_one_or_none()
    if path is None or not os.path.isfile(path):
        # Not archived, or the media volume isn't mounted into this container.
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="video/x-matroska")


@router.post("/played/{video_id}", status_code=204)
def mark_played_in_jellyfin(
    video_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Mark an archived video played in the user's Jellyfin, after they've
    watched it in the in-page player — so Jellyfin doesn't keep offering it."""
    account = db.execute(select(JellyfinAccount).where(JellyfinAccount.user_id == user.id)).scalar_one_or_none()
    item_id = db.execute(
        select(DownloadLink.jellyfin_item_id).where(DownloadLink.user_id == user.id, DownloadLink.video_id == video_id)
    ).scalar_one_or_none()
    if account is None or not account.jellyfin_user_id or not item_id:
        raise HTTPException(status_code=404)
    client = JellyfinClient(base_url=account.base_url, api_key=decrypt_token(account.api_key_encrypted))
    try:
        client.mark_played(item_id, account.jellyfin_user_id)
    except JellyfinError as exc:
        logger.warning("Couldn't mark %s played in Jellyfin for user %s: %s", video_id, user.id, exc)
        raise HTTPException(status_code=502, detail="Jellyfin request failed") from exc
    return Response(status_code=204)
