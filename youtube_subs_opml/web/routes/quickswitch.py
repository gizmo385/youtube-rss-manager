"""Data for the quick switcher (Cmd/Ctrl-K palette).

The switcher's page list is static and lives in the client; only the user's
channels need the database. Fetched once when the palette first opens and
filtered client-side, so there's no per-keystroke round trip.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Channel, Subscription, User

router = APIRouter(tags=["quickswitch"])


@router.get("/quickswitch/channels.json")
def quickswitch_channels(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Every channel the user subscribes to, for jump-to-channel search."""
    rows = db.execute(
        select(Channel.channel_id, Channel.title)
        .join(Subscription, Subscription.channel_id == Channel.channel_id)
        .where(Subscription.user_id == user.id)
        .order_by(func.lower(Channel.title))
    ).all()
    return JSONResponse(
        [{"id": cid, "title": title or cid} for cid, title in rows]
    )
