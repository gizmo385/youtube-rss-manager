from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...downloader.naming import CANONICAL_SUBDIR, LIBRARIES_SUBDIR
from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Category, JellyfinAccount, OpmlToken, User, YoutubeAccount
from ..services.crypto import decrypt_token, encrypt_token
from ..services.jellyfin import JellyfinClient
from ..services.prefs import (
    LINK_TARGETS,
    parse_link_target,
    parse_required_int,
)
from ..services.sync import sync_account
from ..templating import templates

router = APIRouter(tags=["settings"])


@router.get("/settings")
def settings_page(
    request: Request,
    jellyfin_test: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    accounts = db.execute(
        select(YoutubeAccount)
        .where(YoutubeAccount.user_id == user.id)
    ).scalars().all()

    opml_token = db.execute(
        select(OpmlToken).where(OpmlToken.user_id == user.id)
    ).scalar_one_or_none()

    categories = db.execute(
        select(Category)
        .where(Category.user_id == user.id)
        .order_by(Category.name)
    ).scalars().all()

    jellyfin = db.execute(
        select(JellyfinAccount).where(JellyfinAccount.user_id == user.id)
    ).scalar_one_or_none()

    from ..services.stats import shell_stats

    settings = get_settings()
    # The subtree an admin points this user's Jellyfin library at. The relative
    # subpath is the only structure the app imposes; the absolute path just shows
    # where it lands inside the container's media root. Kept in sync with the
    # worker's layout via the shared naming constants.
    library_subpath = f"{LIBRARIES_SUBDIR}/{user.id}"
    library_path = f"{settings.media_root}/{library_subpath}"

    return templates.TemplateResponse(
        request,
        "settings.html",
        context={
            "user": user,
            "accounts": accounts,
            "opml_token": opml_token,
            "categories": categories,
            "base_url": settings.base_url,
            "jellyfin": jellyfin,
            "jellyfin_test": jellyfin_test,
            "library_path": library_path,
            "library_subpath": library_subpath,
            "canonical_subdir": CANONICAL_SUBDIR,
            "media_root": settings.media_root,
            "link_targets": LINK_TARGETS,
            "max_duration_minutes": user.max_duration_seconds // 60,
            "active_nav": "settings",
            "stats": shell_stats(user, db),
        },
    )


@router.post("/sync/{account_id}")
def trigger_sync(
    account_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    account = db.execute(
        select(YoutubeAccount).where(
            YoutubeAccount.id == account_id,
            YoutubeAccount.user_id == user.id,
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="YouTube account not found")

    sync_account(account, db, get_settings())
    db.commit()

    # Newly-synced subscriptions have no cached feed yet; nudge a poll so their
    # feed URLs warm in seconds rather than 503ing until the next interval.
    from ..services.scheduler import trigger_poll_soon
    trigger_poll_soon()

    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/defaults")
async def update_defaults(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    user.include_shorts = "include_shorts" in form
    user.include_live = "include_live" in form

    # Archive defaults (the root of the cascade — never NULL).
    user.download_enabled = "download_enabled" in form
    user.generate_podcast = "generate_podcast" in form
    user.keep_last_n = parse_required_int(form.get("keep_last_n"), 15)
    user.max_duration_seconds = (
        parse_required_int(form.get("max_duration_minutes"), 0) * 60
    )
    user.link_target = parse_link_target(form.get("link_target"), allow_inherit=False)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/jellyfin")
async def update_jellyfin(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Save the user's Jellyfin connection.

    The API key is write-only in the UI: a blank field on an existing account
    leaves the stored key untouched, so the page can render without echoing the
    secret back. Creating a new account requires a key.
    """
    form = await request.form()
    base_url = str(form.get("base_url", "")).strip()
    api_key = str(form.get("api_key", "")).strip()
    jellyfin_user_id = str(form.get("jellyfin_user_id", "")).strip()

    if not base_url:
        raise HTTPException(status_code=400, detail="Jellyfin base URL is required")

    account = db.execute(
        select(JellyfinAccount).where(JellyfinAccount.user_id == user.id)
    ).scalar_one_or_none()

    if account is None:
        if not api_key:
            raise HTTPException(status_code=400, detail="An API key is required")
        db.add(
            JellyfinAccount(
                user_id=user.id,
                base_url=base_url,
                api_key_encrypted=encrypt_token(api_key),
                jellyfin_user_id=jellyfin_user_id,
            )
        )
    else:
        account.base_url = base_url
        account.jellyfin_user_id = jellyfin_user_id
        if api_key:
            account.api_key_encrypted = encrypt_token(api_key)

    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/jellyfin/test")
def test_jellyfin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Verify stored Jellyfin credentials against the live instance."""
    account = db.execute(
        select(JellyfinAccount).where(JellyfinAccount.user_id == user.id)
    ).scalar_one_or_none()
    if account is None:
        return RedirectResponse("/settings?jellyfin_test=missing", status_code=303)

    client = JellyfinClient(
        base_url=account.base_url,
        api_key=decrypt_token(account.api_key_encrypted),
    )
    if client.verify():
        account.last_verified_at = func.now()
        db.commit()
        return RedirectResponse("/settings?jellyfin_test=ok", status_code=303)
    return RedirectResponse("/settings?jellyfin_test=fail", status_code=303)


@router.post("/settings/opml-token/rotate")
def rotate_opml_token(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    token_row = db.execute(
        select(OpmlToken).where(OpmlToken.user_id == user.id)
    ).scalar_one_or_none()

    new_token = secrets.token_urlsafe(32)

    if token_row is None:
        db.add(OpmlToken(user_id=user.id, token=new_token))
    else:
        token_row.token = new_token

    db.commit()
    return RedirectResponse("/settings", status_code=303)
