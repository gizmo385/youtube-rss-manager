from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Category, Channel, ChannelCategory, Subscription, User
from ..templating import templates

router = APIRouter(prefix="/categories", tags=["categories"])


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


async def _category_name_form(request: Request) -> str:
    form = await request.form()
    name = form.get("name")
    if not name or not str(name).strip():
        raise HTTPException(status_code=400, detail="Name is required")
    return str(name)


def _get_owned_category(category_id: int, user: User, db: Session) -> Category:
    category = db.get(Category, category_id)
    if category is None or category.user_id != user.id:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


def _categories_with_counts(user: User, db: Session) -> list[dict]:
    """Load categories with their channel counts."""
    categories = db.execute(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.name)
    ).scalars().all()

    counts = dict(
        db.execute(
            select(ChannelCategory.category_id, func.count())
            .where(ChannelCategory.user_id == user.id)
            .group_by(ChannelCategory.category_id)
        ).all()
    )

    return [
        {"category": cat, "channel_count": counts.get(cat.id, 0)}
        for cat in categories
    ]


def _category_list_response(
    request: Request, user: User, db: Session
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/category_list.html",
        context={
            "categories_with_counts": _categories_with_counts(user, db),
            "user": user,
        },
    )


# --- routes ---


@router.get("")
def list_categories(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if _is_htmx(request):
        return _category_list_response(request, user, db)
    # Full-page fallback redirects to channels page (categories tab)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/channels?tab=categories", status_code=303)


@router.post("")
async def create_category(
    request: Request,
    name: str = Depends(_category_name_form),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    slug = slugify(name)
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid category name")

    existing = db.execute(
        select(Category).where(
            Category.user_id == user.id,
            Category.slug == slug,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Category already exists")

    db.add(Category(user_id=user.id, name=name.strip(), slug=slug))
    db.commit()

    return _category_list_response(request, user, db)


@router.put("/{category_id}")
async def update_category(
    category_id: int,
    request: Request,
    name: str = Depends(_category_name_form),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)

    slug = slugify(name)
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid category name")

    conflict = db.execute(
        select(Category).where(
            Category.user_id == user.id,
            Category.slug == slug,
            Category.id != category_id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise HTTPException(status_code=409, detail="Category slug already taken")

    category.name = name.strip()
    category.slug = slug
    db.commit()

    return _category_list_response(request, user, db)


@router.delete("/{category_id}")
def delete_category(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)
    db.delete(category)
    db.commit()

    return _category_list_response(request, user, db)


@router.patch("/{category_id}/include-shorts")
async def update_category_shorts(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)
    form = await request.form()
    value = form.get("include_shorts", "inherit")
    if value == "true":
        category.include_shorts = True
    elif value == "false":
        category.include_shorts = False
    else:
        category.include_shorts = None
    db.commit()

    return _category_list_response(request, user, db)


@router.patch("/{category_id}/include-live")
async def update_category_live(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)
    form = await request.form()
    value = form.get("include_live", "inherit")
    if value == "true":
        category.include_live = True
    elif value == "false":
        category.include_live = False
    else:
        category.include_live = None
    db.commit()

    return _category_list_response(request, user, db)


@router.get("/{category_id}/channels")
def category_channels(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)

    channels = db.execute(
        select(Channel)
        .join(ChannelCategory, ChannelCategory.channel_id == Channel.channel_id)
        .where(
            ChannelCategory.user_id == user.id,
            ChannelCategory.category_id == category_id,
        )
        .order_by(Channel.title)
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "partials/category_channels.html",
        context={"channels": channels, "category": category},
    )


def _available_channels_query(user_id: int, category_id: int, db: Session):
    """Channels the user subscribes to that are NOT in this category."""
    in_category = select(ChannelCategory.channel_id).where(
        ChannelCategory.user_id == user_id,
        ChannelCategory.category_id == category_id,
    )
    return db.execute(
        select(Channel)
        .join(Subscription, Subscription.channel_id == Channel.channel_id)
        .where(
            Subscription.user_id == user_id,
            Subscription.ignored == False,  # noqa: E712
            ~Channel.channel_id.in_(in_category),
        )
        .order_by(Channel.title)
    ).scalars().all()


@router.get("/{category_id}/available-channels")
def available_channels(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)
    channels = _available_channels_query(user.id, category_id, db)

    return templates.TemplateResponse(
        request,
        "partials/category_add_channels.html",
        context={"channels": channels, "category": category},
    )


@router.post("/{category_id}/add-channel")
async def add_channel_to_category(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    category = _get_owned_category(category_id, user, db)
    form = await request.form()
    channel_id = form.get("channel_id")
    search = form.get("search", "")
    if not channel_id:
        raise HTTPException(status_code=400)

    existing = db.execute(
        select(ChannelCategory).where(
            ChannelCategory.user_id == user.id,
            ChannelCategory.channel_id == str(channel_id),
            ChannelCategory.category_id == category_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(ChannelCategory(
            user_id=user.id,
            channel_id=str(channel_id),
            category_id=category_id,
        ))
        db.commit()

    channels = _available_channels_query(user.id, category_id, db)
    return templates.TemplateResponse(
        request,
        "partials/category_add_channels.html",
        context={"channels": channels, "category": category, "search": search},
    )
