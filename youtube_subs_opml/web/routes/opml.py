from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from youtube_subs_opml.opml import build_opml
from youtube_subs_opml.youtube import Subscription as SubData

from ..db import get_db
from ..models import Category, Channel, ChannelCategory, OpmlToken, Subscription, User

router = APIRouter(prefix="/opml", tags=["opml"])


def _resolve_include_shorts(
    sub_pref: bool | None,
    cat_pref: bool | None,
    user_pref: bool,
) -> bool:
    """Cascade: subscription > category > user. NULL means inherit."""
    if sub_pref is not None:
        return sub_pref
    if cat_pref is not None:
        return cat_pref
    return user_pref


def _validate_token(token: str, db: Session) -> OpmlToken:
    opml_token = db.execute(
        select(OpmlToken).where(OpmlToken.token == token)
    ).scalar_one_or_none()
    if opml_token is None:
        raise HTTPException(status_code=404)
    return opml_token


@router.get("/{token}/all.opml")
def opml_all(
    token: str,
    db: Session = Depends(get_db),
) -> Response:
    opml_token = _validate_token(token, db)

    user = db.get(User, opml_token.user_id)

    rows = db.execute(
        select(Channel, Subscription)
        .join(Subscription, Subscription.channel_id == Channel.channel_id)
        .where(
            Subscription.user_id == opml_token.user_id,
            Subscription.ignored == False,  # noqa: E712
        )
        .order_by(Channel.title)
    ).all()

    subs = [
        SubData(
            channel_id=ch.channel_id,
            title=ch.title,
            description=ch.description,
            include_shorts=_resolve_include_shorts(
                sub.include_shorts, None, user.include_shorts
            ),
        )
        for ch, sub in rows
    ]
    xml = build_opml(subs, title="All Subscriptions")
    return Response(content=xml, media_type="application/xml")


@router.get("/{token}/{slug}.opml")
def opml_by_category(
    token: str,
    slug: str,
    db: Session = Depends(get_db),
) -> Response:
    opml_token = _validate_token(token, db)

    user = db.get(User, opml_token.user_id)

    category = db.execute(
        select(Category).where(
            Category.user_id == opml_token.user_id,
            Category.slug == slug,
        )
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=404)

    rows = db.execute(
        select(Channel, Subscription)
        .join(
            ChannelCategory,
            ChannelCategory.channel_id == Channel.channel_id,
        )
        .join(
            Subscription,
            and_(
                Subscription.user_id == ChannelCategory.user_id,
                Subscription.channel_id == ChannelCategory.channel_id,
            ),
        )
        .where(
            ChannelCategory.category_id == category.id,
            ChannelCategory.user_id == opml_token.user_id,
            Subscription.ignored == False,  # noqa: E712
        )
        .order_by(Channel.title)
    ).all()

    subs = [
        SubData(
            channel_id=ch.channel_id,
            title=ch.title,
            description=ch.description,
            include_shorts=_resolve_include_shorts(
                sub.include_shorts, category.include_shorts, user.include_shorts
            ),
        )
        for ch, sub in rows
    ]
    xml = build_opml(subs, title=category.name)
    return Response(content=xml, media_type="application/xml")
