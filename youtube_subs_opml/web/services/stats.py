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
    Video,
)


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
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.user_id == user.id)
    ).scalar_one()

    categories = db.execute(
        select(func.count())
        .select_from(Category)
        .where(Category.user_id == user.id)
    ).scalar_one()

    # Downloads are global; scope disk + failures to this user's subscriptions.
    status_rows = db.execute(
        select(Download.status, func.count(), func.coalesce(func.sum(Download.file_size_bytes), 0))
        .select_from(Download)
        .join(Video, Video.video_id == Download.video_id)
        .join(Subscription, Subscription.channel_id == Video.channel_id)
        .where(Subscription.user_id == user.id)
        .group_by(Download.status)
    ).all()

    archived_bytes = sum(size for status, _, size in status_rows if status == "complete")
    failed = sum(count for status, count, _ in status_rows if status == "failed")

    return {
        "channels": channels,
        "categories": categories,
        "archived_bytes": int(archived_bytes),
        "archived_human": format_bytes(int(archived_bytes)),
        "failed": failed,
    }
