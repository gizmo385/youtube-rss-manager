"""Aggregate figures for the shell sub-bar (channels / categories / archived).

Rendered under the header on every logged-in screen, so it's kept to two small
grouped queries rather than anything the page already computes.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Category,
    Download,
    Subscription,
    User,
)
from .sources import followed_video_ids


def format_bytes(num: int) -> str:
    """Human-readable size, e.g. ``412.0 GB``. Bytes are shown whole."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def shell_stats(user: User, db: Session) -> dict:
    """Totals for the sub-bar: channel/category counts, archived size, failures."""
    channels = db.execute(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    ).scalar_one()

    categories = db.execute(select(func.count()).select_from(Category).where(Category.user_id == user.id)).scalar_one()

    # Downloads are global; scope disk to every subscription, ignored or not.
    archived_bytes: int = (
        db.execute(
            select(func.sum(Download.file_size_bytes)).where(
                Download.status == "complete",
                Download.video_id.in_(followed_video_ids(user.id, include_ignored=True)),
            )
        ).scalar_one()
        or 0
    )
    # Failures, though, only count where the Downloads tab can show them (and
    # so retry or dismiss them): a failure in an ignored channel would otherwise
    # badge the nav with nothing the user can act on.
    failed = db.execute(
        select(func.count())
        .select_from(Download)
        .where(
            Download.status == "failed",
            Download.video_id.in_(followed_video_ids(user.id)),
        )
    ).scalar_one()

    return {
        "channels": channels,
        "categories": categories,
        "archived_bytes": archived_bytes,
        "archived_human": format_bytes(archived_bytes),
        "failed": failed,
    }
