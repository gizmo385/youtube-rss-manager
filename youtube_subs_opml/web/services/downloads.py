"""Downloader visibility and manual retry, scoped to a user's subscriptions.

``downloads`` rows are global (one file, shared across subscribers), so every
query here joins through the user's non-ignored subscriptions — a user only
ever sees, or can retry, a download for a channel they follow.

"Retry" just resets a terminal row back to ``pending`` (clearing the attempt
count, backoff, skip reason and last error); the downloader worker claims
pending rows on its next pass, so nothing else needs poking.
"""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import Channel, ChannelCategory, Download, Subscription, Video

# Terminal states a user can meaningfully retry from.
_RETRYABLE_STATUSES = ("failed", "skipped")

# The statuses a Download row can hold, for the status filter dropdown.
DOWNLOAD_STATUSES = ("pending", "downloading", "complete", "failed", "skipped")

# Human-friendly labels for the UI. The raw values are what the worker writes;
# these are only ever for display (pills, dropdowns, tooltips).
STATUS_LABELS: dict[str, str] = {
    "pending": "Queued",
    "downloading": "Downloading",
    "complete": "Archived",
    "failed": "Failed",
    "skipped": "Skipped",
}

# Skip reasons (Download.skip_reason) → short label + a plain-English tooltip.
SKIP_REASON_LABELS: dict[str, str] = {
    "too_long": "Too long",
    "too_short": "Too short",
    "unavailable": "Unavailable",
    "members_only": "Members only",
    "geo_blocked": "Region-blocked",
    "no_subscribers": "Not wanted",
    "short": "Short",
}
SKIP_REASON_HELP: dict[str, str] = {
    "too_long": "Longer than the max-duration cap set for this channel.",
    "too_short": "Shorter than the min-duration floor set for this channel.",
    "unavailable": "Private, removed, age-restricted, or otherwise unfetchable.",
    "members_only": "Requires a paid channel membership to watch.",
    "geo_blocked": "Not available in this server's region.",
    "no_subscribers": "No subscriber currently has archiving enabled for this channel.",
    "short": "A YouTube Short, excluded from the archive for this channel.",
}

# Rows per page in the download-history table.
PER_PAGE = 25


def _visible(user_id: int):
    """A join onto the user's non-ignored subscriptions for a Download query."""
    return (
        select(Download)
        .join(Video, Video.video_id == Download.video_id)
        .join(
            Subscription,
            and_(
                Subscription.channel_id == Video.channel_id,
                Subscription.user_id == user_id,
                Subscription.ignored == False,  # noqa: E712
            ),
        )
    )


def status_counts(db: Session, user_id: int) -> dict[str, int]:
    """Count of downloads by status within the user's scope."""
    rows = db.execute(
        select(Download.status, func.count())
        .select_from(Download)
        .join(Video, Video.video_id == Download.video_id)
        .join(
            Subscription,
            and_(
                Subscription.channel_id == Video.channel_id,
                Subscription.user_id == user_id,
                Subscription.ignored == False,  # noqa: E712
            ),
        )
        .group_by(Download.status)
    ).all()
    return {status: count for status, count in rows}


def recoverable_count(db: Session, user_id: int) -> int:
    """How many downloads 'Retry all recoverable' would requeue (failed + unavailable)."""
    return db.execute(
        select(func.count())
        .select_from(Download)
        .join(Video, Video.video_id == Download.video_id)
        .join(
            Subscription,
            and_(
                Subscription.channel_id == Video.channel_id,
                Subscription.user_id == user_id,
                Subscription.ignored == False,  # noqa: E712
            ),
        )
        .where(
            or_(
                Download.status == "failed",
                and_(
                    Download.status == "skipped",
                    Download.skip_reason == "unavailable",
                ),
            )
        )
    ).scalar_one()


def subscribed_channels(db: Session, user_id: int) -> list[dict]:
    """The user's non-ignored channels, for the channel filter dropdown."""
    rows = db.execute(
        select(Channel.channel_id, Channel.title)
        .join(
            Subscription,
            and_(
                Subscription.channel_id == Channel.channel_id,
                Subscription.user_id == user_id,
                Subscription.ignored == False,  # noqa: E712
            ),
        )
        .order_by(func.lower(Channel.title))
    ).all()
    return [{"channel_id": cid, "title": title or cid} for cid, title in rows]


def _filtered_query(
    user_id: int,
    *,
    channel_id: str | None,
    category_id: int | None,
    status: str | None,
    q: str | None,
):
    """Base SELECT over (Video, Download, Channel) in scope, with filters applied."""
    stmt = (
        select(Video, Download, Channel)
        .join(Download, Download.video_id == Video.video_id)
        .join(Channel, Channel.channel_id == Video.channel_id)
        .join(
            Subscription,
            and_(
                Subscription.channel_id == Video.channel_id,
                Subscription.user_id == user_id,
                Subscription.ignored == False,  # noqa: E712
            ),
        )
    )
    if channel_id:
        stmt = stmt.where(Video.channel_id == channel_id)
    if status:
        stmt = stmt.where(Download.status == status)
    if q:
        stmt = stmt.where(Video.title.ilike(f"%{q}%"))
    if category_id:
        stmt = stmt.join(
            ChannelCategory,
            and_(
                ChannelCategory.channel_id == Video.channel_id,
                ChannelCategory.user_id == user_id,
                ChannelCategory.category_id == category_id,
            ),
        )
    return stmt


def list_downloads(
    db: Session,
    user_id: int,
    *,
    channel_id: str | None = None,
    category_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int = PER_PAGE,
) -> dict:
    """A page of the user's whole download history, filtered and newest-first.

    Returns the rows plus everything the template needs to render pagination:
    ``total`` (matching the filters), clamped ``page``, ``pages``, ``has_prev``/
    ``has_next`` and the 1-based ``start``/``end`` indices of this page.
    """
    base = _filtered_query(
        user_id, channel_id=channel_id, category_id=category_id, status=status, q=q
    )
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)

    rows_stmt = (
        base.order_by(
            Video.published_at.desc().nullslast(),
            Download.created_at.desc(),
            Video.video_id,
        )
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    rows = []
    for video, download, channel in db.execute(rows_stmt).all():
        rows.append(
            {
                "video_id": video.video_id,
                "title": video.title or video.video_id,
                "channel_title": channel.title or channel.channel_id,
                "channel_id": channel.channel_id,
                "status": download.status,
                "skip_reason": download.skip_reason,
                "attempts": download.attempts,
                "last_error": download.last_error,
                "published_at": video.published_at,
                "file_size_bytes": download.file_size_bytes,
            }
        )

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
        "start": 0 if total == 0 else (page - 1) * per_page + 1,
        "end": min(page * per_page, total),
    }


def _reset(download: Download) -> None:
    """Return a terminal row to the front of the queue for a fresh attempt."""
    download.status = "pending"
    download.skip_reason = None
    download.attempts = 0
    download.next_attempt_at = None
    download.last_error = None


def retry_one(db: Session, user_id: int, video_id: str) -> bool:
    """Requeue a single failed/skipped download. Returns False if not eligible.

    Scoped to the user's subscriptions, so a token/URL can't requeue arbitrary
    videos, and only terminal rows are reset (never one mid-download).
    """
    download = db.execute(
        _visible(user_id).where(Download.video_id == video_id)
    ).scalar_one_or_none()
    if download is None or download.status not in _RETRYABLE_STATUSES:
        return False
    _reset(download)
    db.commit()
    return True


def retry_all_recoverable(db: Session, user_id: int) -> int:
    """Requeue every plausibly-recoverable download in scope.

    That's failures plus ``unavailable`` skips — the latter is where a *transient*
    probe error (bot-check, throttling) that exhausted its retries lands, so it's
    exactly the bucket most likely to be a false negative. Deliberate skips
    (too_long, members_only, geo_blocked, ...) are left alone; retry them
    individually if you really mean to.
    """
    rows = db.execute(
        _visible(user_id).where(
            or_(
                Download.status == "failed",
                and_(
                    Download.status == "skipped",
                    Download.skip_reason == "unavailable",
                ),
            )
        )
    ).scalars().all()
    for download in rows:
        _reset(download)
    if rows:
        db.commit()
    return len(rows)
