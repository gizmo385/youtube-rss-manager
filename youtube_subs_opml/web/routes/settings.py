from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Category, OpmlToken, User, YoutubeAccount
from ..services.sync import sync_account
from ..templating import templates

router = APIRouter(tags=["settings"])


@router.get("/settings")
def settings_page(
    request: Request,
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

    return templates.TemplateResponse(
        request,
        "settings.html",
        context={
            "user": user,
            "accounts": accounts,
            "opml_token": opml_token,
            "categories": categories,
            "base_url": get_settings().base_url,
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
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/include-shorts")
async def update_include_shorts(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    user.include_shorts = "include_shorts" in form
    db.commit()
    return RedirectResponse("/settings", status_code=303)


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
