from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Category, Channel, ChannelCategory, Subscription, User
from ..templating import templates
from .categories import _categories_with_counts

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

    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)
    return templates.TemplateResponse(request, "channels.html", context=ctx)


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
