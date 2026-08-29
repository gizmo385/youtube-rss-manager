from __future__ import annotations

import logging
from datetime import datetime, timezone

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
from ..models import (
    Category,
    Channel,
    ChannelCategory,
    Download,
    OpmlToken,
    Subscription,
    User,
    Video,
    YoutubeAccount,
)
from ..services.crypto import decrypt_token
from ..services.prefs import (
    LINK_TARGETS,
    parse_inherit_int,
    parse_link_target,
    parse_tristate_bool,
    minutes_to_seconds,
    resolve,
)
from ..services.resolve import resolve_channel_public
from ..services.stats import format_bytes, shell_stats
from ..services.sync import build_google_credentials
from ..templating import templates
from .categories import _categories_with_counts

# Archive prefs editable per subscription, and how each form value is parsed.
# "minutes" fields arrive from the UI in minutes and are stored as seconds.
_ARCHIVE_FIELD_KINDS = {
    "download_enabled": "bool",
    "generate_podcast": "bool",
    "keep_last_n": "int",
    "max_duration_seconds": "minutes",
    "link_target": "link",
}

# Filter chips on the channel list. "All" is the default (no filtering).
_FILTERS = ("All", "Uncategorized", "Archiving", "Failed", "Ignored")


def _parse_archive_value(field: str, value: str | None):
    kind = _ARCHIVE_FIELD_KINDS[field]
    if kind == "bool":
        return parse_tristate_bool(value)
    if kind == "int":
        return parse_inherit_int(value)
    if kind == "minutes":
        return minutes_to_seconds(value)
    return parse_link_target(value, allow_inherit=True)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _ago(dt: datetime | None) -> str:
    """Compact relative age, e.g. ``9h`` / ``2d``. ``—`` when unknown."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 1)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# --- preference resolution helpers -----------------------------------------


def _effective(sub, cats: list[Category], user: User, key: str):
    """Resolve one pref through subscription > category > user.

    ``cats`` is walked in the order given (name order), matching how the feed
    proxy would pick a category context. Returns ``(value, source_label)``.
    """
    own = getattr(sub, key)
    if own is not None:
        return own, "this channel"
    for cat in cats:
        v = getattr(cat, key)
        if v is not None:
            return v, cat.name
    return getattr(user, key), "account default"


def _channel_ids_with_failures(user: User, db: Session) -> set[str]:
    """Channel ids that have at least one failed download (for the list badge)."""
    rows = db.execute(
        select(Video.channel_id)
        .select_from(Download)
        .join(Video, Video.video_id == Download.video_id)
        .join(Subscription, Subscription.channel_id == Video.channel_id)
        .where(Subscription.user_id == user.id, Download.status == "failed")
        .distinct()
    ).scalars().all()
    return set(rows)


# --- list context -----------------------------------------------------------


def _build_list_context(user: User, db: Session, selected: str | None, filt: str) -> dict:
    """Groups of channels for the master list, honouring the active filter.

    Channels are grouped Uncategorized-first, then by category (a channel in
    several categories appears under each). Empty groups are dropped.
    """
    if filt not in _FILTERS:
        filt = "All"

    rows = db.execute(
        select(Subscription, Channel)
        .join(Channel, Subscription.channel_id == Channel.channel_id)
        .where(Subscription.user_id == user.id)
        .order_by(Channel.title)
    ).all()

    categories = db.execute(
        select(Category).where(Category.user_id == user.id).order_by(Category.name)
    ).scalars().all()
    cat_by_id = {c.id: c for c in categories}

    assignments = db.execute(
        select(ChannelCategory).where(ChannelCategory.user_id == user.id)
    ).scalars().all()
    channel_cats: dict[str, list[Category]] = {}
    for a in assignments:
        cat = cat_by_id.get(a.category_id)
        if cat:
            channel_cats.setdefault(a.channel_id, []).append(cat)
    for cats in channel_cats.values():
        cats.sort(key=lambda c: c.name)

    failures = _channel_ids_with_failures(user, db)

    # Build a light per-channel record for the list.
    records = []
    for sub, ch in rows:
        cats = channel_cats.get(ch.channel_id, [])
        archive_on, _ = _effective(sub, cats, user, "download_enabled")
        records.append({
            "channel_id": ch.channel_id,
            "title": ch.title,
            "ignored": sub.ignored,
            "cats": cats,
            "cat_count": len(cats),
            "archive_on": bool(archive_on),
            "has_failure": ch.channel_id in failures,
            "is_manual": sub.account_id is None,
        })

    # Apply the active filter to the pool before grouping.
    if filt == "Uncategorized":
        pool = [r for r in records if not r["cats"]]
    elif filt == "Archiving":
        pool = [r for r in records if r["archive_on"]]
    elif filt == "Failed":
        pool = [r for r in records if r["has_failure"]]
    elif filt == "Ignored":
        pool = [r for r in records if r["ignored"]]
    else:
        pool = records

    groups = []
    uncat = [r for r in pool if not r["cats"]]
    if uncat:
        groups.append({"name": "Uncategorized", "count": len(uncat), "channels": uncat})
    for cat in categories:
        members = [r for r in pool if cat in r["cats"]]
        if members:
            groups.append({"name": cat.name, "count": len(members), "channels": members})

    return {
        "groups": groups,
        "total": len(records),
        "filters": _FILTERS,
        "active_filter": filt,
        "selected_id": selected,
        "categories": categories,
        "link_targets": LINK_TARGETS,
    }


def _first_channel_id(list_ctx: dict) -> str | None:
    for g in list_ctx["groups"]:
        if g["channels"]:
            return g["channels"][0]["channel_id"]
    return None


# --- detail context ---------------------------------------------------------


def _pref_choice(sub, cats, user, key, label, opts, *, minutes=False):
    """A segmented pref row: own value + effective-value subtext."""
    value, source = _effective(sub, cats, user, key)
    own = getattr(sub, key)

    def fmt(v):
        if v is True:
            return "On"
        if v is False:
            return "Off"
        if v is None:
            return "—"
        if minutes and isinstance(v, int):
            return str(v // 60)
        return str(v)

    return {
        "label": label,
        "effective": f"Now {fmt(value)} · from {source}",
        "options": [{"label": l, "value": val, "on": own == raw}
                    for l, val, raw in opts],
    }


def _pref_number(sub, cats, user, key, label, unit, hint, *, minutes=False):
    value, source = _effective(sub, cats, user, key)
    own = getattr(sub, key)
    if minutes:
        own_display = "" if own is None else str(own // 60)
        eff_display = value // 60 if value is not None else 0
    else:
        own_display = "" if own is None else str(own)
        eff_display = value if value is not None else 0
    return {
        "label": label,
        "effective": f"{hint} · effective {eff_display} from {source}",
        "unit": unit,
        "value": own_display,
    }


def _archive_activity(channel_id: str, db: Session) -> list[dict]:
    """Recent download rows for a channel, newest first, as activity items."""
    rows = db.execute(
        select(Download, Video)
        .join(Video, Video.video_id == Download.video_id)
        .where(Video.channel_id == channel_id)
        .order_by(Download.created_at.desc())
        .limit(15)
    ).all()

    items = []
    for dl, vid in rows:
        dur = f"{vid.duration_seconds // 60} min" if vid.duration_seconds else None
        if dl.status == "complete":
            status = "ok"
            when = dl.completed_at.strftime("%Y-%m-%d") if dl.completed_at else "recently"
            meta = f"Downloaded {when}" + (f" · {dur}" if dur else "")
            size = format_bytes(dl.file_size_bytes or 0)
        elif dl.status == "failed":
            status = "fail"
            err = (dl.last_error or "download error").splitlines()[0][:60]
            meta = f"Failed — {err}" + (f" ({dl.attempts} attempts)" if dl.attempts else "")
            size = "—"
        elif dl.status == "skipped":
            status = "skip"
            meta = f"Skipped — {dl.skip_reason or 'excluded'}"
            size = "—"
        else:
            status = "idle"
            meta = dl.status.capitalize()
            size = "—"
        items.append({
            "title": vid.title or vid.video_id,
            "meta": meta,
            "size": size,
            "status": status,
        })
    return items


def _channel_detail(user: User, db: Session, channel_id: str) -> dict | None:
    """Full detail context for one channel, or None if not subscribed."""
    row = db.execute(
        select(Subscription, Channel)
        .join(Channel, Subscription.channel_id == Channel.channel_id)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id == channel_id,
        )
    ).one_or_none()
    if row is None:
        return None
    sub, ch = row

    cats = db.execute(
        select(Category)
        .join(ChannelCategory, ChannelCategory.category_id == Category.id)
        .where(
            ChannelCategory.user_id == user.id,
            ChannelCategory.channel_id == channel_id,
        )
        .order_by(Category.name)
    ).scalars().all()
    cats = list(cats)

    # Disk + count over completed downloads for this channel.
    disk, count = db.execute(
        select(
            func.coalesce(func.sum(Download.file_size_bytes), 0),
            func.count(),
        )
        .select_from(Download)
        .join(Video, Video.video_id == Download.video_id)
        .where(Video.channel_id == channel_id, Download.status == "complete")
    ).one()

    last_video = db.execute(
        select(func.max(Video.published_at)).where(Video.channel_id == channel_id)
    ).scalar_one_or_none()

    # Assignable categories (not already assigned).
    assigned_ids = {c.id for c in cats}
    all_categories = db.execute(
        select(Category).where(Category.user_id == user.id).order_by(Category.name)
    ).scalars().all()
    assignable = [c for c in all_categories if c.id not in assigned_ids]

    token = db.execute(
        select(OpmlToken).where(OpmlToken.user_id == user.id)
    ).scalar_one_or_none()
    base_url = get_settings().base_url
    feed_url = (
        f"{base_url}/feed/{token.token}/{channel_id}.xml"
        if token else None
    )

    tri = [("Inherit", "inherit", None), ("On", "true", True), ("Off", "false", False)]
    incl = [("Inherit", "inherit", None), ("Include", "true", True), ("Exclude", "false", False)]
    link_opts = [("Inherit", "inherit", None)] + [(lt, lt, lt) for lt in LINK_TARGETS]

    return {
        "channel_id": ch.channel_id,
        "title": ch.title,
        "is_manual": sub.account_id is None,
        "youtube_url": f"https://www.youtube.com/channel/{ch.channel_id}",
        "cats": cats,
        "assignable_categories": assignable,
        "topics": ch.youtube_topics or [],
        "disk_human": format_bytes(int(disk or 0)),
        "video_count": count or 0,
        "last_video": _ago(last_video),
        "feed_url": feed_url,
        "feed_settings": [
            _pref_choice(sub, cats, user, "include_shorts", "Include Shorts", incl),
            _pref_choice(sub, cats, user, "include_live", "Include premieres & livestreams", incl),
        ],
        "archive_settings": [
            _pref_choice(sub, cats, user, "download_enabled", "Download to Jellyfin", tri),
        ],
        "archive_numbers": [
            _pref_number(sub, cats, user, "keep_last_n", "Keep last N", "videos", "Blank inherits"),
            _pref_number(sub, cats, user, "max_duration_seconds", "Max duration", "min",
                         "Blank inherits, 0 = no limit", minutes=True),
        ],
        "archive_choices": [
            _pref_choice(sub, cats, user, "generate_podcast", "Podcast audio", tri),
            _pref_choice(sub, cats, user, "link_target", "Feed link target", link_opts),
        ],
        "activity": _archive_activity(channel_id, db),
    }


# --- responses --------------------------------------------------------------


def _list_response(request: Request, user: User, db: Session, selected: str | None, filt: str) -> HTMLResponse:
    ctx = _build_list_context(user, db, selected, filt)
    ctx["user"] = user
    return templates.TemplateResponse(request, "partials/channel_list.html", context=ctx)


def _detail_response(
    request: Request,
    user: User,
    db: Session,
    channel_id: str,
    *,
    oob_list: bool = False,
    filt: str = "All",
) -> HTMLResponse:
    detail = _channel_detail(user, db, channel_id)
    ctx: dict = {"user": user, "detail": detail}
    if oob_list:
        list_ctx = _build_list_context(user, db, channel_id, filt)
        ctx.update(list_ctx)
        ctx["oob_list"] = True
    return templates.TemplateResponse(request, "partials/channel_detail.html", context=ctx)


def _stage_response_args(form) -> tuple[str, str | None]:
    """Extract (filter, selected) from a write request's form."""
    filt = form.get("filter") or "All"
    channel_ids = form.getlist("channel_ids")
    selected = form.get("selected") or (channel_ids[0] if channel_ids else None)
    return filt, selected


def _write_response(request: Request, form, user: User, db: Session, *, grouping_changed: bool) -> HTMLResponse:
    """Render the right partial after a write, based on where it came from.

    ``return=detail`` writes come from the detail pane and swap it (optionally
    OOB-refreshing the list when grouping changed); everything else came from
    the list and swaps the list.
    """
    filt, selected = _stage_response_args(form)
    if form.get("return") == "detail" and selected:
        return _detail_response(request, user, db, selected, oob_list=grouping_changed, filt=filt)
    return _list_response(request, user, db, selected, filt)


# --- board context ----------------------------------------------------------


def _build_board_context(user: User, db: Session) -> dict:
    """Columns for the kanban board: Uncategorized + one per category."""
    list_ctx = _build_list_context(user, db, None, "All")
    categories = list_ctx["categories"]

    rows = db.execute(
        select(Subscription, Channel)
        .join(Channel, Subscription.channel_id == Channel.channel_id)
        .where(Subscription.user_id == user.id)
        .order_by(Channel.title)
    ).all()
    assignments = db.execute(
        select(ChannelCategory).where(ChannelCategory.user_id == user.id)
    ).scalars().all()
    cats_by_channel: dict[str, list[int]] = {}
    for a in assignments:
        cats_by_channel.setdefault(a.channel_id, []).append(a.category_id)

    cat_channels: dict[int, list] = {cat.id: [] for cat in categories}
    uncategorized: list[dict] = []
    for sub, ch in rows:
        if sub.ignored:
            continue
        cat_ids = cats_by_channel.get(ch.channel_id, [])
        card = {"channel_id": ch.channel_id, "title": ch.title}
        if not cat_ids:
            uncategorized.append({**card, "other_category_count": 0})
        else:
            for cid in cat_ids:
                if cid in cat_channels:
                    cat_channels[cid].append({**card, "other_category_count": len(cat_ids) - 1})

    columns = [{"id": None, "name": "Uncategorized", "channels": uncategorized}]
    for cat in categories:
        columns.append({"id": cat.id, "name": cat.name, "channels": cat_channels.get(cat.id, [])})
    return {"columns": columns, "categories": categories}


# --- routes: read -----------------------------------------------------------


@router.get("")
def list_channels(
    request: Request,
    filter: str = "All",
    selected: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    list_ctx = _build_list_context(user, db, selected, filter)
    if not selected:
        selected = _first_channel_id(list_ctx)
        list_ctx["selected_id"] = selected

    if _is_htmx(request):
        list_ctx["user"] = user
        return templates.TemplateResponse(request, "partials/channel_list.html", context=list_ctx)

    ctx = dict(list_ctx)
    ctx["user"] = user
    ctx["active_nav"] = "channels"
    ctx["stats"] = shell_stats(user, db)
    ctx["detail"] = _channel_detail(user, db, selected) if selected else None
    return templates.TemplateResponse(request, "channels.html", context=ctx)


@router.get("/list")
def channel_list_partial(
    request: Request,
    filter: str = "All",
    selected: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _list_response(request, user, db, selected, filter)


@router.get("/{channel_id}/detail")
def channel_detail_partial(
    channel_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    detail = _channel_detail(user, db, channel_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return templates.TemplateResponse(
        request, "partials/channel_detail.html", context={"user": user, "detail": detail}
    )


@router.get("/board")
def get_board(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ctx = _build_board_context(user, db)
    ctx["user"] = user
    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/kanban_board.html", context=ctx)
    ctx["active_nav"] = "board"
    ctx["stats"] = shell_stats(user, db)
    return templates.TemplateResponse(request, "board.html", context=ctx)


# --- routes: write ----------------------------------------------------------


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
            db.add(ChannelCategory(
                user_id=user.id,
                channel_id=str(cid),
                category_id=category_id,
            ))

    db.commit()
    return _write_response(request, form, user, db, grouping_changed=True)


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
    return _write_response(request, form, user, db, grouping_changed=True)


@router.post("/include-shorts")
async def set_include_shorts(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    if not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel")

    db.execute(
        update(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_([str(c) for c in channel_ids]),
        )
        .values(include_shorts=parse_tristate_bool(form.get("include_shorts")))
    )
    db.commit()
    return _write_response(request, form, user, db, grouping_changed=False)


@router.post("/include-live")
async def set_include_live(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    if not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel")

    db.execute(
        update(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_([str(c) for c in channel_ids]),
        )
        .values(include_live=parse_tristate_bool(form.get("include_live")))
    )
    db.commit()
    return _write_response(request, form, user, db, grouping_changed=False)


@router.post("/archive-pref")
async def set_archive_pref(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Set one archive preference on one or more subscriptions.

    Generic over the field (validated against an allowlist) rather than one
    endpoint per pref. NULL means inherit from the channel's categories, then
    the user default.
    """
    form = await request.form()
    channel_ids = form.getlist("channel_ids")
    field = form.get("field", "")
    if not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel")
    if field not in _ARCHIVE_FIELD_KINDS:
        raise HTTPException(status_code=400, detail="Unknown preference")

    parsed = _parse_archive_value(field, form.get("value"))
    db.execute(
        update(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.channel_id.in_([str(c) for c in channel_ids]),
        )
        .values(**{field: parsed})
    )
    db.commit()
    # download_enabled changes the "Archiving" grouping/badge; refresh the list.
    grouping_changed = field == "download_enabled"
    return _write_response(request, form, user, db, grouping_changed=grouping_changed)


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
    return _write_response(request, form, user, db, grouping_changed=True)


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
            creds = build_google_credentials(
                decrypt_token(account.refresh_token_encrypted), get_settings()
            )
            resolved = resolve_channel(creds, raw)
        else:
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
        db.add(Channel(
            channel_id=resolved.channel_id,
            title=resolved.title,
            description=resolved.description,
            youtube_topics=resolved.topics,
        ))
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
        db.add(Subscription(
            user_id=user.id,
            channel_id=resolved.channel_id,
            account_id=None,
        ))

    db.commit()

    filt = form.get("filter") or "All"
    return _list_response(request, user, db, resolved.channel_id, filt)


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

    filt = form.get("filter") or "All"
    return _list_response(request, user, db, None, filt)
