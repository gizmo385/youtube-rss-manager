"""OpenTelemetry metrics recorded as things happen (feed polls, downloads).

Library-wide figures (videos per channel/category, durations, archive size) are
snapshot gauges read from the database instead; see
``web.services.library_metrics``.

Instruments are no-ops unless the process runs under ``opentelemetry-instrument``
with a metrics exporter configured (the image leaves it off by default).

Attributes are kept low-cardinality except ``channel``/``channel_id``, which are
bounded by the number of subscribed channels (a few hundred at most).
"""

from __future__ import annotations

from opentelemetry import metrics

meter = metrics.get_meter("youtube_subs_opml")

# --- Feed polling (web process) --------------------------------------------

feed_fetch_attempts = meter.create_counter(
    "yt_rss_feed_fetch_attempts",
    unit="{request}",
    description=(
        "Every HTTP request for a channel feed, including retries. `status` is "
        "the HTTP status code or 'network_error'; `attempt` counts from 1."
    ),
)
feed_polls = meter.create_counter(
    "yt_rss_feed_polls",
    unit="{poll}",
    description=(
        "Final result of polling one channel's feed, after retries. `outcome` is "
        "'ok', 'http_error' or 'network_error'; `status` is the last HTTP status."
    ),
)
feed_api_fallbacks = meter.create_counter(
    "yt_rss_feed_api_fallbacks",
    unit="{request}",
    description=(
        "Data API fetches of a channel's uploads after its RSS feed failed, per "
        "channel. `outcome` is 'ok', 'error', or 'rate_limited' (skipped, no "
        "quota used: this channel fell back too recently)."
    ),
)
feed_poll_duration = meter.create_histogram(
    "yt_rss_feed_poll_duration",
    unit="s",
    description="Time to fetch one channel's feed, including retries and backoff.",
)
feed_new_videos = meter.create_counter(
    "yt_rss_feed_new_videos",
    unit="{video}",
    description="Videos seen for the first time on a channel's feed.",
)

# --- Downloader --------------------------------------------------------------

downloads = meter.create_counter(
    "yt_rss_downloads",
    unit="{download}",
    description=(
        "Downloader attempts by `outcome` ('complete', 'skipped', 'retry', 'failed', 'error') and skip `reason`."
    ),
)
download_duration = meter.create_histogram(
    "yt_rss_download_duration",
    unit="s",
    description="Time spent on one download attempt: probe, checks and fetch.",
)
downloaded_bytes = meter.create_counter(
    "yt_rss_downloaded_bytes",
    unit="By",
    description="Bytes written by completed downloads, by `kind` ('video', 'audio').",
)
