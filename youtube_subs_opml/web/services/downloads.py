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

from ..models import Channel, Download, Subscription, Video

# Skip reasons that are deliberate/expected rather than failures, so they're
# kept out of the "problem" list (a channel excluding Shorts would otherwise
# flood it). Everything else — unavailable, too_long, members_only,
# geo_blocked — is worth surfacing.
_MUTED_SKIP_REASONS = ("short", "no_subscribers")

# Terminal states a user can meaningfully retry from.
_RETRYABLE_STATUSES = ("failed", "skipped")


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


def problem_downloads(db: Session, user_id: int, *, limit: int = 100) -> list[dict]:
    """Failed or notably-skipped downloads the user might want to retry.

    Newest first, so recent breakage is at the top. ``last_error`` is the raw
    yt-dlp stderr captured by the worker — the actual reason a probe/download
    fell over.
    """
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
        .where(
            or_(
                Download.status == "failed",
                and_(
                    Download.status == "skipped",
                    Download.skip_reason.notin_(_MUTED_SKIP_REASONS),
                ),
            )
        )
        .order_by(Video.published_at.desc().nullslast())
        .limit(limit)
    )
    out: list[dict] = []
    for video, download, channel in db.execute(stmt).all():
        out.append(
            {
                "video_id": video.video_id,
                "title": video.title or video.video_id,
                "channel_title": channel.title or channel.channel_id,
                "status": download.status,
                "skip_reason": download.skip_reason,
                "attempts": download.attempts,
                "last_error": download.last_error,
                "published_at": video.published_at,
            }
        )
    return out


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
