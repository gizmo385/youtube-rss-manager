from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from google.auth.exceptions import RefreshError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from youtube_subs_opml.youtube import ChannelLookupError, resolve_channel

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Category, Channel, ChannelCategory, Subscription, User, YoutubeAccount
from ..services.crypto import decrypt_token
from ..services.resolve import resolve_channel_public
from ..services.sync import build_google_credentials
from ..templating import templates
from .categories import _categories_with_counts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _build_channel_context(user: User, db: Session) -> dict:
    """Build the template context for the channel list."""
    rows = db.execute(
        select(Subscription, Channel)
        .join(Channel, Subscription.channel_id == Channel.channel_id)
        .where(Subscription.user_id == user.id)
        .order_by(Channel.title)
    ).all()

    categories = db.execute(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.name)
    ).scalars().all()

    assignments = db.execute(
        select(ChannelCategory)
        .where(ChannelCategory.user_id == user.id)
    ).scalars().all()

    # Build a map: channel_id -> list of category names
    cat_by_id = {c.id: c for c in categories}
    channel_cats: dict[str, list[Category]] = {}
    for a in assignments:
        cat = cat_by_id.get(a.category_id)
        if cat:
            channel_cats.setdefault(a.channel_id, []).append(cat)

    # Build channel list with metadata
    channels = []
    for sub, ch in rows:
        assigned = channel_cats.get(ch.channel_id, [])
        channels.append({
            "channel_id": ch.channel_id,
            "title": ch.title,
            "ignored": sub.ignored,
            "include_shorts": sub.include_shorts,
            "include_live": sub.include_live,
            "is_manual": sub.account_id is None,
            "categories": sorted(assigned, key=lambda c: c.name),
            "assigned_category_ids": {c.id for c in assigned},
            "youtube_topics": ch.youtube_topics or [],
        })

    # Group: uncategorized first, then by category
    uncategorized = [c for c in channels if not c["categories"] and not c["ignored"]]
    ignored = [c for c in channels if c["ignored"]]
    categorized = [c for c in channels if c["categories"] and not c["ignored"]]

    return {
        "channels": channels,
        "uncategorized": uncategorized,
        "categorized": categorized,
        "ignored": ignored,
        "categories": categories,
        "total": len(channels),
    }


def _build_board_context(user: User, db: Session) -> dict:
    """Build template context for the kanban board view."""
    ctx = _build_channel_context(user, db)
    categories = ctx["categories"]
    channels = ctx["channels"]

    # Build columns: uncategorized + one per category
    cat_channels: dict[int, list] = {cat.id: [] for cat in categories}
    uncategorized: list[dict] = []

    for ch in channels:
        if ch["ignored"]:
            continue
        if not ch["categories"]:
            uncategorized.append({**ch, "other_category_count": 0})
        else:
            for cat in ch["categories"]:
                other_count = len(ch["categories"]) - 1
                cat_channels[cat.id].append({**ch, "other_category_count": other_count})

    columns = [{"id": None, "name": "Uncategorized", "channels": uncategorized}]
    for cat in categories:
        columns.append({
            "id": cat.id,
            "name": cat.name,
            "channels": cat_channels.get(cat.id, []),
        })

    return {"columns": columns, "categories": categories}


@router.get("")
def list_channels(
    request: Request,
    tab: str = "channels",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    ctx["active_tab"] = tab
    ctx["categories_with_counts"] = _categories_with_counts(user, db)

    if tab == "board":
        board_ctx = _build_board_context(user, db)
        ctx["columns"] = board_ctx["columns"]

    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)
    return templates.TemplateResponse(request, "channels.html", context=ctx)


@router.get("/board")
def get_board(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ctx = _build_board_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/kanban_board.html", context=ctx)


@router.post("/move")
async def move_channel(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_id = form.get("channel_id")
    from_category_id = form.get("from_category_id", "")
    to_category_id = form.get("to_category_id", "")

    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id required")

    # Remove from source category
    if from_category_id:
        from_id = int(from_category_id)
        row = db.execute(
            select(ChannelCategory).where(
                ChannelCategory.user_id == user.id,
                ChannelCategory.channel_id == str(channel_id),
                ChannelCategory.category_id == from_id,
            )
        ).scalar_one_or_none()
        if row is not None:
            db.delete(row)

    # Add to target category
    if to_category_id:
        to_id = int(to_category_id)
        category = db.get(Category, to_id)
        if category is None or category.user_id != user.id:
            raise HTTPException(status_code=404, detail="Category not found")
        existing = db.execute(
            select(ChannelCategory).where(
                ChannelCategory.user_id == user.id,
                ChannelCategory.channel_id == str(channel_id),
                ChannelCategory.category_id == to_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(ChannelCategory(
                user_id=user.id,
                channel_id=str(channel_id),
                category_id=to_id,
            ))

    db.commit()

    ctx = _build_board_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/kanban_board.html", context=ctx)


@router.post("/assign")
async def assign_channels(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    category_id = form.get("category_id")

    if not channel_ids or not category_id:
        raise HTTPException(status_code=400, detail="Select channels and a category")

    category_id = int(category_id)

    # Verify category ownership
    category = db.get(Category, category_id)
    if category is None or category.user_id != user.id:
        raise HTTPException(status_code=404, detail="Category not found")

    for cid in channel_ids:
        existing = db.execute(
            select(ChannelCategory).where(
                ChannelCategory.user_id == user.id,
                ChannelCategory.channel_id == str(cid),
                ChannelCategory.category_id == category_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                ChannelCategory(
                    user_id=user.id,
                    channel_id=str(cid),
                    category_id=category_id,
                )
            )

    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


@router.post("/unassign")
async def unassign_channels(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    category_id = form.get("category_id")

    if not channel_ids or not category_id:
        raise HTTPException(status_code=400, detail="Select channels and a category")

    category_id = int(category_id)

    for cid in channel_ids:
        row = db.execute(
            select(ChannelCategory).where(
                ChannelCategory.user_id == user.id,
                ChannelCategory.channel_id == str(cid),
                ChannelCategory.category_id == category_id,
            )
        ).scalar_one_or_none()
        if row is not None:
            db.delete(row)

    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


@router.post("/include-shorts")
async def set_include_shorts(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    value = form.get("include_shorts", "inherit")

    if not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel")

    if value == "true":
        shorts_val = True
    elif value == "false":
        shorts_val = False
    else:
        shorts_val = None

    db.execute(
        update(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_([str(c) for c in channel_ids]),
        )
        .values(include_shorts=shorts_val)
    )
    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


@router.post("/include-live")
async def set_include_live(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    value = form.get("include_live", "inherit")

    if not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel")

    if value == "true":
        live_val = True
    elif value == "false":
        live_val = False
    else:
        live_val = None

    db.execute(
        update(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_([str(c) for c in channel_ids]),
        )
        .values(include_live=live_val)
    )
    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


@router.post("/ignore")
async def ignore_channels(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    ignored = form.get("ignored", "true") == "true"

    if not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel")

    db.execute(
        update(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_([str(c) for c in channel_ids]),
        )
        .values(ignored=ignored)
    )
    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


@router.post("/add")
async def add_manual_channel(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    raw = str(form.get("channel_input", "")).strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Enter a channel URL, handle, or ID")

    account = db.execute(
        select(YoutubeAccount).where(YoutubeAccount.user_id == user.id).limit(1)
    ).scalar_one_or_none()

    try:
        if account is not None:
            # Use the connected account's API credentials (richer metadata).
            creds = build_google_credentials(
                decrypt_token(account.refresh_token_encrypted), get_settings()
            )
            resolved = resolve_channel(creds, raw)
        else:
            # No account connected: resolve from public HTTP (no auth needed).
            resolved = resolve_channel_public(raw)
    except ChannelLookupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RefreshError:
        logger.error("Refresh token revoked for account %s", account.id)
        raise HTTPException(
            status_code=400,
            detail="YouTube account needs to be re-connected in Settings.",
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=400,
            detail="Could not reach YouTube to look up that channel.",
        )

    channel = db.get(Channel, resolved.channel_id)
    if channel is None:
        db.add(
            Channel(
                channel_id=resolved.channel_id,
                title=resolved.title,
                description=resolved.description,
                youtube_topics=resolved.topics,
            )
        )
    else:
        channel.title = resolved.title
        channel.description = resolved.description
        channel.youtube_topics = resolved.topics
        channel.last_seen_at = func.now()

    existing = db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.channel_id == resolved.channel_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            Subscription(
                user_id=user.id,
                channel_id=resolved.channel_id,
                account_id=None,
            )
        )

    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


@router.post("/remove")
async def remove_manual_channel(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_id = form.get("channel_id")
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id required")

    sub = db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.channel_id == str(channel_id),
        )
    ).scalar_one_or_none()

    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.account_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove a synced subscription. Unsubscribe on YouTube instead.",
        )

    db.delete(sub)
    db.commit()

    ctx = _build_channel_context(user, db)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)
