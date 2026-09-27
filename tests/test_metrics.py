"""OpenTelemetry metrics: what the poller and downloader record, and the
library snapshot behind the gauges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from youtube_subs_opml.downloader import worker
from youtube_subs_opml.opml import FEED_URL
from youtube_subs_opml.web.db import Base
from youtube_subs_opml.web.models import (
    Category,
    Channel,
    ChannelCategory,
    Download,
    Subscription,
    User,
    Video,
)
from youtube_subs_opml.web.services import library_metrics, poller, uploads_api

FEED = (
    b'<?xml version="1.0"?>'
    b'<feed xmlns="http://www.w3.org/2005/Atom" '
    b'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
    b"<entry><yt:videoId>vidMETRIC01</yt:videoId><title>Hi</title>"
    b"<published>2026-08-20T09:00:00+00:00</published></entry></feed>"
)

# The global meter provider can only be set once per process, and the
# instruments in youtube_subs_opml.metrics bind to it lazily, so one reader
# serves the whole module. Counters are cumulative: each test uses its own
# channel ids and reads only its own points.
_reader = InMemoryMetricReader()
otel_metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))


def _points(name: str, **attrs: object) -> list:
    data = _reader.get_metrics_data()
    points = []
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    points += [
                        p
                        for p in metric.data.data_points
                        if all((p.attributes or {}).get(k) == v for k, v in attrs.items())
                    ]
    return points


def _total(name: str, **attrs: object) -> float:
    return sum(p.value for p in _points(name, **attrs))


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setenv("POLL_CHANNEL_DELAY_SECONDS", "0")
    monkeypatch.setenv("POLL_MAX_RETRIES", "2")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def scripted_client(script: dict[str, list]) -> httpx.Client:
    """Serves a scripted list of responses (or exceptions) per URL, in order."""

    def handler(request: httpx.Request) -> httpx.Response:
        item = script[str(request.url)].pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item[0], content=item[1])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retried_404_counts_every_attempt_and_one_ok_poll(db):
    cid = "UCmetric000000000000001"
    db.add(Channel(channel_id=cid, title="Metric Channel"))
    db.commit()
    url = FEED_URL.format(channel_id=cid)

    new = poller.poll_channel(cid, db, scripted_client({url: [(404, b""), (200, FEED)]}))

    assert new == 1
    assert _total("yt_rss_feed_fetch_attempts", status="404", attempt=1, platform="youtube") >= 1
    assert _total("yt_rss_feed_fetch_attempts", status="200", attempt=2, platform="youtube") >= 1
    assert _total("yt_rss_feed_polls", channel_id=cid, outcome="ok", status="200") == 1
    assert _total("yt_rss_feed_polls", channel_id=cid, channel="Metric Channel") == 1
    assert _total("yt_rss_feed_new_videos", channel_id=cid) == 1


def test_persistent_404_is_a_failed_poll_with_its_status(db):
    cid = "UCmetric000000000000002"
    url = FEED_URL.format(channel_id=cid)

    assert poller.poll_channel(cid, db, scripted_client({url: [(404, b"")] * 3})) == 0

    assert _total("yt_rss_feed_polls", channel_id=cid, outcome="http_error", status="404") == 1
    assert _total("yt_rss_feed_polls", channel_id=cid, outcome="ok") == 0
    # No Channel row: the channel id stands in for the title.
    assert _total("yt_rss_feed_polls", channel_id=cid, channel=cid) == 1
    assert _total("yt_rss_feed_new_videos", channel_id=cid) == 0


def test_network_errors_are_counted_separately(db):
    cid = "UCmetric000000000000003"
    url = FEED_URL.format(channel_id=cid)
    boom = httpx.ConnectError("boom", request=httpx.Request("GET", url))

    poller.poll_channel(cid, db, scripted_client({url: [boom, boom, boom]}))

    assert _total("yt_rss_feed_polls", channel_id=cid, outcome="network_error") == 1
    assert _total("yt_rss_feed_fetch_attempts", status="network_error", attempt=3) >= 1


def test_api_fallback_is_counted_per_channel_and_outcome(db, monkeypatch):
    from youtube_subs_opml.web import config

    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    config.get_settings.cache_clear()
    monkeypatch.setattr(poller, "_api_fallback_at", {})
    cid = "UCmetric000000000000004"
    db.add(Channel(channel_id=cid, title="Fallback Channel"))
    db.commit()
    client = scripted_client(
        {
            FEED_URL.format(channel_id=cid): [(404, b"")] * 6,
            uploads_api.uploads_url(cid): [(200, b'{"items": []}')],
        }
    )

    poller.poll_channel(cid, db, client)
    poller.poll_channel(cid, db, client)  # inside the hour: skipped

    # The RSS failure is still recorded as such; the fallback is counted beside it.
    assert _total("yt_rss_feed_polls", channel_id=cid, outcome="http_error") == 2
    assert _total("yt_rss_feed_api_fallbacks", channel_id=cid, channel="Fallback Channel", outcome="ok") == 1
    assert _total("yt_rss_feed_api_fallbacks", channel_id=cid, outcome="rate_limited") == 1


def test_record_download_classifies_outcomes():
    complete = Download(video_id="v1", status="complete", file_size_bytes=100, audio_size_bytes=7)
    retry = Download(video_id="v2", status="pending")
    skipped = Download(video_id="v3", status="skipped", skip_reason="too_long")
    stuck = Download(video_id="v4", status="downloading")
    before = _total("yt_rss_downloaded_bytes", kind="video")

    assert worker._record_download(complete, 1.0) == "complete"
    assert worker._record_download(retry, 1.0) == "retry"
    assert worker._record_download(skipped, 1.0) == "skipped"
    assert worker._record_download(stuck, 1.0) == "error"

    assert _total("yt_rss_downloads", outcome="skipped", reason="too_long") >= 1
    assert _total("yt_rss_downloaded_bytes", kind="video") - before == 100


def _observed(snapshot: dict, name: str, **attrs: str) -> list[float]:
    return [v for v, a in snapshot[name] if all(a.get(k) == val for k, val in attrs.items())]


def test_library_snapshot(db):
    now = datetime(2026, 9, 26, tzinfo=UTC)
    db.add(User(id=1, oidc_sub="sub", email="me@example.com"))
    db.add_all(
        [
            Channel(channel_id="UCa", title="Planes"),
            Channel(channel_id="UCb", title="Trains"),
            Channel(channel_id="nebula:c", title="Nebby", platform="nebula"),
        ]
    )
    db.flush()
    db.add_all(
        [
            Subscription(user_id=1, channel_id="UCa"),
            Subscription(user_id=1, channel_id="UCb"),
            Subscription(user_id=1, channel_id="nebula:c", ignored=True),
            Category(id=10, user_id=1, name="Aviation", slug="aviation"),
        ]
    )
    db.flush()
    db.add(ChannelCategory(user_id=1, channel_id="UCa", category_id=10))
    db.add_all(
        [
            Video(video_id="a1", channel_id="UCa", published_at=now - timedelta(days=2), duration_seconds=30),
            Video(video_id="a2", channel_id="UCa", published_at=now - timedelta(days=90), duration_seconds=600),
            Video(video_id="b1", channel_id="UCb", published_at=now - timedelta(days=10)),
        ]
    )
    db.flush()
    db.add_all(
        [
            Download(video_id="a1", status="complete", file_size_bytes=1000, audio_size_bytes=50),
            Download(video_id="a2", status="skipped", skip_reason="too_long"),
        ]
    )
    db.commit()

    snap = library_metrics.collect(db, now=now)

    assert _observed(snap, "yt_rss_channels", platform="youtube", ignored="false") == [2]
    assert _observed(snap, "yt_rss_channels", platform="nebula", ignored="true") == [1]
    assert _observed(snap, "yt_rss_channel_videos", channel="Planes") == [2]
    assert _observed(snap, "yt_rss_channel_recent_uploads", channel="Planes") == [1]
    assert _observed(snap, "yt_rss_channel_days_since_upload", channel="Trains") == [10]
    assert _observed(snap, "yt_rss_channel_avg_video_duration", channel="Planes") == [315]
    # Trains has no probed durations, so no average.
    assert _observed(snap, "yt_rss_channel_avg_video_duration", channel="Trains") == []
    assert _observed(snap, "yt_rss_category_channels", category="Aviation") == [1]
    assert _observed(snap, "yt_rss_category_videos", category="Aviation") == [2]
    assert _observed(snap, "yt_rss_category_total_duration", category="Aviation") == [630]
    # Channels in no category are grouped rather than dropped.
    assert _observed(snap, "yt_rss_category_channels", category="(uncategorized)") == [2]
    assert _observed(snap, "yt_rss_videos_by_length", length="<1m") == [1]
    assert _observed(snap, "yt_rss_videos_by_length", length="5-15m") == [1]
    assert _observed(snap, "yt_rss_videos", duration_known="false") == [1]
    assert _observed(snap, "yt_rss_download_states", status="skipped", reason="too_long") == [1]
    assert _observed(snap, "yt_rss_archive_bytes", channel="Planes", kind="video") == [1000]
    assert _observed(snap, "yt_rss_archive_bytes", channel="Planes", kind="audio") == [50]
