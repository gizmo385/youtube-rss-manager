from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..forms import form_text
from ..models import (
    Category,
    Channel,
    ChannelCategory,
    Download,
    Subscription,
    User,
)
from ..services import sponsorblock
from ..services.prefs import (
    LINK_TARGET_LABELS,
    LINK_TARGETS,
    minutes_to_seconds,
    parse_inherit_int,
    parse_link_target,
    parse_tristate_bool,
)
from ..services.sources import video_sources
from ..templating import templates

router = APIRouter(prefix="/categories", tags=["categories"])

# Same archive prefs as subscriptions carry, editable at the category level.
_ARCHIVE_FIELD_KINDS = {
    "include_shorts": "bool",
    "include_live": "bool",
    "download_enabled": "bool",
    "generate_podcast": "bool",
    "keep_last_n": "int",
    "keep_last_n_audio": "int",
    "max_duration_seconds": "minutes",
    "min_duration_seconds": "minutes",
    "link_target": "link",
    "sponsorblock": "sponsorblock",
    "podcast_sponsorblock": "podcast_sponsorblock",
}


def _parse_archive_value(field: str, value: str | None):
    kind = _ARCHIVE_FIELD_KINDS[field]
    if kind == "bool":
        return parse_tristate_bool(value)
    if kind == "int":
        return parse_inherit_int(value)
    if kind == "minutes":
        return minutes_to_seconds(value)
    if kind == "sponsorblock":
        return sponsorblock.parse_mode(value, allow_inherit=True)
    if kind == "podcast_sponsorblock":
        return sponsorblock.parse_mode(value, allow_inherit=True, modes=sponsorblock.PODCAST_MODES)
    return parse_link_target(value, allow_inherit=True)


def _format_bytes(num: int) -> str:
    """Human-readable size for the disk-usage column."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
    name = form_text(form, "name")
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    return name


def _get_owned_category(category_id: int, user: User, db: Session) -> Category:
    category = db.get(Category, category_id)
    if category is None or category.user_id != user.id:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


_ON_OFF = [("On", "true", True), ("Off", "false", False)]
_INCLUDE = [("Include", "true", True), ("Exclude", "false", False)]

# Every setting a category can override, in the order the editor lists them:
# (field, label, kind, extra). For "choice", extra is the options as
# (label, form value, stored value); for numbers, (unit, what 0 means, the
# card's summary of an override, and of an override of 0). Minutes fields are
# stored as seconds.
_SETTINGS: list[tuple[str, str, str, Any]] = [
    ("download_enabled", "Archive videos", "choice", _ON_OFF),
    (
        "keep_last_n",
        "Keep last N videos",
        "number",
        ("videos", "every video", "Keep last {} videos", "Keep every video"),
    ),
    (
        "keep_last_n_audio",
        "Keep last N podcast episodes",
        "number",
        ("episodes", "every episode", "Keep last {} podcast episodes", "Keep every podcast episode"),
    ),
    (
        "max_duration_seconds",
        "Longest video to archive",
        "minutes",
        ("min", "no limit", "Longest video: {} min", "No limit on video length"),
    ),
    (
        "min_duration_seconds",
        "Shortest video to archive",
        "minutes",
        ("min", "no minimum", "Shortest video: {} min", "No minimum video length"),
    ),
    ("include_shorts", "Shorts in feeds", "choice", _INCLUDE),
    ("include_live", "Premieres & livestreams", "choice", _INCLUDE),
    ("generate_podcast", "Podcast audio", "choice", _ON_OFF),
    ("link_target", "Feed link target", "choice", [(LINK_TARGET_LABELS[lt], lt, lt) for lt in LINK_TARGETS]),
    (
        "sponsorblock",
        "SponsorBlock in player",
        "choice",
        [("Off", "off", "off"), ("Show", "show", "show"), ("Skip", "skip", "skip")],
    ),
    (
        "podcast_sponsorblock",
        "SponsorBlock in podcasts",
        "choice",
        [("Off", "off", "off"), ("Remove", "remove", "remove")],
    ),
]


def _number_text(value: int | None, kind: str, zero: str) -> str | None:
    """A stored number as the editor shows it: minutes for durations, and
    words for the 0 that means "no limit"."""
    if value is None:
        return None
    if kind == "minutes":
        value //= 60
    return zero if value == 0 else str(value)


def _category_settings(cat: Category, user: User) -> list[dict]:
    """One row per setting for the category editor and its summary.

    Each row says whether the category overrides the setting, what the account
    default it would otherwise follow is (so "inherit" is never a mystery),
    and, for an override, the short text the card shows for it.
    """
    rows = []
    for field, label, kind, extra in _SETTINGS:
        own = getattr(cat, field)
        account = getattr(user, field)
        row: dict[str, Any] = {"field": field, "label": label, "kind": kind, "overridden": own is not None}
        if kind == "choice":
            names = {raw: name for name, _value, raw in extra}
            row["options"] = [{"label": name, "value": value, "selected": own == raw} for name, value, raw in extra]
            row["inherit_label"] = f"Account: {names.get(account, account)}"
            row["summary"] = f"{label}: {names.get(own, own)}" if own is not None else None
        else:
            unit, zero, summary, zero_summary = extra
            account_text = _number_text(account, kind, zero)
            inherit_label = f"Account: {account_text}"
            if field == "keep_last_n_audio" and (cat.keep_last_n is not None or account is None):
                # A blank audio window follows the video window before it
                # looks at the account's audio default (see archive._audio_keep).
                inherit_label = "Same as videos"
            shown = _number_text(own, kind, zero)
            row["unit"] = unit
            row["value"] = "" if own is None else str(own // 60 if kind == "minutes" else own)
            row["inherit_label"] = inherit_label
            row["summary"] = None
            if shown is not None:
                row["summary"] = zero_summary if shown == zero else summary.format(shown)
        rows.append(row)
    return rows


# How many channel names a card lists before it says "and N more".
_CHANNEL_PREVIEW = 8


def _categories_with_counts(user: User, db: Session) -> list[dict]:
    """Load categories with their channel counts."""
    categories = db.execute(select(Category).where(Category.user_id == user.id).order_by(Category.name)).scalars().all()

    counts = dict(
        db.execute(
            select(ChannelCategory.category_id, func.count())
            .where(ChannelCategory.user_id == user.id)
            .group_by(ChannelCategory.category_id)
        )
        .tuples()
        .all()
    )

    # Disk used by completed downloads for the channels and playlists in each
    # category, counting a video in two of them once.
    vs = video_sources()
    in_category = (
        select(ChannelCategory.category_id, vs.c.video_id)
        .join(vs, vs.c.source_id == ChannelCategory.channel_id)
        .where(ChannelCategory.user_id == user.id)
        .distinct()
        .subquery()
    )
    disk = dict(
        db.execute(
            select(
                in_category.c.category_id,
                func.coalesce(func.sum(Download.file_size_bytes), 0),
            )
            .join(Download, Download.video_id == in_category.c.video_id)
            .where(Download.status == "complete")
            .group_by(in_category.c.category_id)
        )
        .tuples()
        .all()
    )

    titles: dict[int, list[str]] = {}
    for category_id, title in db.execute(
        select(ChannelCategory.category_id, Channel.title)
        .join(Channel, Channel.channel_id == ChannelCategory.channel_id)
        .where(ChannelCategory.user_id == user.id)
        .order_by(Channel.title)
    ).tuples():
        titles.setdefault(category_id, []).append(title)

    items = []
    for cat in categories:
        settings = _category_settings(cat, user)
        names = titles.get(cat.id, [])
        items.append(
            {
                "category": cat,
                "channel_count": counts.get(cat.id, 0),
                "disk_bytes": int(disk.get(cat.id) or 0),
                "disk_human": _format_bytes(int(disk.get(cat.id) or 0)),
                "settings": settings,
                "overrides": [s["summary"] for s in settings if s["overridden"]],
                "channel_names": names[:_CHANNEL_PREVIEW],
                "more_channels": max(0, len(names) - _CHANNEL_PREVIEW),
            }
        )
    return items


def _category_list_response(request: Request, user: User, db: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/category_list.html",
        context={
            "categories_with_counts": _categories_with_counts(user, db),
            "user": user,
            "link_targets": LINK_TARGETS,
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
    from ..services.stats import shell_stats

    return templates.TemplateResponse(
        request,
        "categories.html",
        context={
            "categories_with_counts": _categories_with_counts(user, db),
            "user": user,
            "link_targets": LINK_TARGETS,
            "active_nav": "categories",
            "stats": shell_stats(user, db),
        },
    )


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


@router.patch("/{category_id}/archive-pref")
async def update_category_archive(
    category_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Set one archive preference on a category. NULL means inherit from user."""
    category = _get_owned_category(category_id, user, db)
    form = await request.form()
    field = form_text(form, "field") or ""
    if field not in _ARCHIVE_FIELD_KINDS:
        raise HTTPException(status_code=400, detail="Unknown preference")

    setattr(category, field, _parse_archive_value(field, form_text(form, "value")))
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

    channels = (
        db.execute(
            select(Channel)
            .join(ChannelCategory, ChannelCategory.channel_id == Channel.channel_id)
            .where(
                ChannelCategory.user_id == user.id,
                ChannelCategory.category_id == category_id,
            )
            .order_by(Channel.title)
        )
        .scalars()
        .all()
    )

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
    return (
        db.execute(
            select(Channel)
            .join(Subscription, Subscription.channel_id == Channel.channel_id)
            .where(
                Subscription.user_id == user_id,
                Subscription.ignored == False,  # noqa: E712
                ~Channel.channel_id.in_(in_category),
            )
            .order_by(Channel.title)
        )
        .scalars()
        .all()
    )


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
    channel_id = form_text(form, "channel_id")
    search = form_text(form, "search") or ""
    if not channel_id:
        raise HTTPException(status_code=400)

    existing = db.execute(
        select(ChannelCategory).where(
            ChannelCategory.user_id == user.id,
            ChannelCategory.channel_id == channel_id,
            ChannelCategory.category_id == category_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            ChannelCategory(
                user_id=user.id,
                channel_id=channel_id,
                category_id=category_id,
            )
        )
        db.commit()

    channels = _available_channels_query(user.id, category_id, db)
    return templates.TemplateResponse(
        request,
        "partials/category_add_channels.html",
        context={"channels": channels, "category": category, "search": search},
    )
