"""Library-wide OpenTelemetry gauges: what's in the archive rather than what
just happened (that's ``youtube_subs_opml.metrics``).

The SDK reads every gauge on each metrics export (once a minute by default).
Rather than query per gauge per export, all gauges share one snapshot taken by
a handful of grouped queries and reused for ``_SNAPSHOT_TTL``.

Durations come from the downloader's metadata probe (the RSS feed doesn't carry
them), so duration figures cover probed videos only; ``yt_rss_videos`` reports
how many that is.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy import Integer, cast, func, literal, select
from sqlalchemy.orm import Session

from youtube_subs_opml.metrics import meter

from ..db import get_session_factory
from ..models import Category, Channel, ChannelCategory, Download, Subscription, Video

logger = logging.getLogger(__name__)

_SNAPSHOT_TTL = 300.0
_UNCATEGORIZED = "(uncategorized)"

# Upper bounds (seconds) and labels for the video-length distribution.
_LENGTH_BUCKETS: list[tuple[float, str]] = [
    (60, "<1m"),
    (5 * 60, "1-5m"),
    (15 * 60, "5-15m"),
    (30 * 60, "15-30m"),
    (60 * 60, "30-60m"),
    (2 * 60 * 60, "1-2h"),
    (float("inf"), ">2h"),
]

Observations = list[tuple[float, dict[str, str]]]


def _length_bucket(seconds: int) -> str:
    return next(label for bound, label in _LENGTH_BUCKETS if seconds < bound)


def _channel_attrs(channel_id: str, title: str | None, platform: str | None) -> dict[str, str]:
    return {"channel_id": channel_id, "channel": title or channel_id, "platform": platform or "youtube"}


def collect(db: Session, now: datetime | None = None) -> dict[str, Observations]:
    """Compute every gauge's observations. Pure DB reads; no commits."""
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=30)
    out: dict[str, Observations] = {}

    # Subscribed channels; `ignored` only if every subscriber ignores it.
    followed = (
        select(Subscription.channel_id, func.min(cast(Subscription.ignored, Integer)).label("ignored"))
        .group_by(Subscription.channel_id)
        .subquery()
    )
    out["yt_rss_channels"] = [
        (count, {"platform": platform or "youtube", "ignored": str(bool(ignored)).lower()})
        for platform, ignored, count in db.execute(
            select(Channel.platform, followed.c.ignored, func.count())
            .join(followed, followed.c.channel_id == Channel.channel_id)
            .group_by(Channel.platform, followed.c.ignored)
        )
    ]

    # Per subscribed channel: videos, recent uploads, last upload, lengths.
    per_channel = db.execute(
        select(
            Channel.channel_id,
            Channel.title,
            Channel.platform,
            func.count(Video.video_id),
            func.count(Video.video_id).filter(Video.published_at >= recent_cutoff),
            func.max(Video.published_at),
            func.avg(Video.duration_seconds),
        )
        .join(followed, followed.c.channel_id == Channel.channel_id)
        .outerjoin(Video, Video.channel_id == Channel.channel_id)
        .group_by(Channel.channel_id, Channel.title, Channel.platform)
    ).all()
    out["yt_rss_channel_videos"] = []
    out["yt_rss_channel_recent_uploads"] = []
    out["yt_rss_channel_days_since_upload"] = []
    out["yt_rss_channel_avg_video_duration"] = []
    for channel_id, title, platform, videos, recent, last, avg_duration in per_channel:
        attrs = _channel_attrs(channel_id, title, platform)
        out["yt_rss_channel_videos"].append((videos, attrs))
        out["yt_rss_channel_recent_uploads"].append((recent, attrs))
        if last is not None:
            if last.tzinfo is None:  # SQLite drops the zone
                last = last.replace(tzinfo=timezone.utc)
            out["yt_rss_channel_days_since_upload"].append(((now - last).total_seconds() / 86400, attrs))
        if avg_duration is not None:
            out["yt_rss_channel_avg_video_duration"].append((float(avg_duration), attrs))

    # Per category (merged across users by name), plus followed channels that
    # aren't in any category.
    category_of = (
        select(ChannelCategory.channel_id, Category.name.label("category"))
        .join(Category, Category.id == ChannelCategory.category_id)
        .union(
            select(followed.c.channel_id, literal(_UNCATEGORIZED).label("category"))
            .where(followed.c.channel_id.not_in(select(ChannelCategory.channel_id)))
        )
        .subquery()
    )
    per_category = db.execute(
        select(
            category_of.c.category,
            func.count(func.distinct(category_of.c.channel_id)),
            func.count(func.distinct(Video.video_id)),
            func.avg(Video.duration_seconds),
            func.sum(Video.duration_seconds),
        )
        .outerjoin(Video, Video.channel_id == category_of.c.channel_id)
        .group_by(category_of.c.category)
    ).all()
    out["yt_rss_category_channels"] = [(ch, {"category": cat}) for cat, ch, _, _, _ in per_category]
    out["yt_rss_category_videos"] = [(v, {"category": cat}) for cat, _, v, _, _ in per_category]
    out["yt_rss_category_avg_video_duration"] = [
        (float(avg), {"category": cat}) for cat, _, _, avg, _ in per_category if avg is not None
    ]
    out["yt_rss_category_total_duration"] = [
        (float(total), {"category": cat}) for cat, _, _, _, total in per_category if total is not None
    ]

    # Video lengths across the library, and how much of it has been probed.
    durations = db.execute(select(Video.duration_seconds)).scalars().all()
    buckets = {label: 0 for _, label in _LENGTH_BUCKETS}
    for seconds in durations:
        if seconds is not None:
            buckets[_length_bucket(seconds)] += 1
    out["yt_rss_videos_by_length"] = [(n, {"length": label}) for label, n in buckets.items()]
    probed = sum(1 for d in durations if d is not None)
    out["yt_rss_videos"] = [
        (probed, {"duration_known": "true"}),
        (len(durations) - probed, {"duration_known": "false"}),
    ]

    # Download states, and archive size per channel.
    out["yt_rss_download_states"] = [
        (count, {"status": status, "reason": reason or ""})
        for status, reason, count in db.execute(
            select(Download.status, Download.skip_reason, func.count()).group_by(
                Download.status, Download.skip_reason
            )
        )
    ]
    out["yt_rss_archive_bytes"] = []
    for channel_id, title, platform, video_bytes, audio_bytes in db.execute(
        select(
            Channel.channel_id,
            Channel.title,
            Channel.platform,
            func.coalesce(func.sum(Download.file_size_bytes), 0),
            func.coalesce(func.sum(Download.audio_size_bytes), 0),
        )
        .join(Video, Video.channel_id == Channel.channel_id)
        .join(Download, Download.video_id == Video.video_id)
        .where(Download.status == "complete")
        .group_by(Channel.channel_id, Channel.title, Channel.platform)
    ):
        attrs = _channel_attrs(channel_id, title, platform)
        out["yt_rss_archive_bytes"].append((int(video_bytes), {**attrs, "kind": "video"}))
        out["yt_rss_archive_bytes"].append((int(audio_bytes), {**attrs, "kind": "audio"}))
    return out


class _Snapshot:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._taken_at = 0.0
        self._data: dict[str, Observations] = {}

    def get(self, name: str) -> Observations:
        with self._lock:
            if time.monotonic() - self._taken_at >= _SNAPSHOT_TTL:
                db = self._session_factory()
                try:
                    self._data = collect(db)
                except Exception:
                    # Keep serving the last good snapshot; retry next TTL.
                    logger.exception("Library metrics snapshot failed")
                finally:
                    db.close()
                self._taken_at = time.monotonic()
            return self._data.get(name, [])


_GAUGES: dict[str, tuple[str, str]] = {
    "yt_rss_channels": ("{channel}", "Followed channels by platform and ignored state."),
    "yt_rss_channel_videos": ("{video}", "Videos recorded per channel."),
    "yt_rss_channel_recent_uploads": ("{video}", "Videos published in the last 30 days per channel."),
    "yt_rss_channel_days_since_upload": ("d", "Days since each channel's newest recorded video."),
    "yt_rss_channel_avg_video_duration": ("s", "Average length of each channel's probed videos."),
    "yt_rss_category_channels": ("{channel}", "Channels per category."),
    "yt_rss_category_videos": ("{video}", "Videos per category."),
    "yt_rss_category_avg_video_duration": ("s", "Average length of probed videos per category."),
    "yt_rss_category_total_duration": ("s", "Total length of probed videos per category."),
    "yt_rss_videos_by_length": ("{video}", "Probed videos by length bucket."),
    "yt_rss_videos": ("{video}", "Videos by whether their duration has been probed yet."),
    "yt_rss_download_states": ("{download}", "Download rows by status and skip reason."),
    "yt_rss_archive_bytes": ("By", "Archived bytes per channel, by kind (video/audio)."),
}

_registered = False


def register(session_factory: Callable[[], Session] | None = None) -> None:
    """Create the gauges. Idempotent; call once at app startup."""
    global _registered
    if _registered:
        return
    snapshot = _Snapshot(session_factory or get_session_factory())

    def callback_for(name: str) -> Callable[[CallbackOptions], Iterable[Observation]]:
        def callback(options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(value, attrs) for value, attrs in snapshot.get(name)]

        return callback

    for name, (unit, description) in _GAUGES.items():
        meter.create_observable_gauge(name, callbacks=[callback_for(name)], unit=unit, description=description)
    _registered = True
