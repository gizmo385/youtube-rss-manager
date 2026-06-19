from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from youtube_subs_opml.opml import build_opml
from youtube_subs_opml.youtube import Subscription as SubData

from ..config import get_settings
from ..db import get_db
from ..models import Category, Channel, ChannelCategory, OpmlToken, Subscription

router = APIRouter(prefix="/opml", tags=["opml"])


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

    # Every channel routes through the feed proxy, which resolves the Shorts
    # cascade at request time — so the OPML only needs the channel list here.
    channels = db.execute(
        select(Channel)
        .join(Subscription, Subscription.channel_id == Channel.channel_id)
        .where(
            Subscription.user_id == opml_token.user_id,
            Subscription.ignored == False,  # noqa: E712
        )
        .order_by(Channel.title)
    ).scalars().all()

    subs = [
        SubData(channel_id=ch.channel_id, title=ch.title, description=ch.description)
        for ch in channels
    ]
    xml = build_opml(
        subs,
        title="All Subscriptions",
        proxy_base_url=get_settings().base_url,
        opml_token=token,
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/{token}/{slug}.opml")
def opml_by_category(
    token: str,
    slug: str,
    db: Session = Depends(get_db),
) -> Response:
    opml_token = _validate_token(token, db)

    category = db.execute(
        select(Category).where(
            Category.user_id == opml_token.user_id,
            Category.slug == slug,
        )
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=404)

    channels = db.execute(
        select(Channel)
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
    ).scalars().all()

    subs = [
        SubData(channel_id=ch.channel_id, title=ch.title, description=ch.description)
        for ch in channels
    ]
    xml = build_opml(
        subs,
        title=category.name,
        proxy_base_url=get_settings().base_url,
        opml_token=token,
        category_slug=slug,
    )
    return Response(content=xml, media_type="application/xml")
