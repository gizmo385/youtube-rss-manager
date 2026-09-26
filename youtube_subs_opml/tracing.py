"""OpenTelemetry tracing for background work.

The containers run under ``opentelemetry-instrument``, which traces incoming
HTTP requests, SQL and outgoing HTTP calls automatically. Scheduled jobs and the
downloader loop have no incoming request for those spans to hang off, so each
unit of that work gets a root span from :data:`tracer`. Without an exporter
configured (the image default), these spans are no-ops.
"""

from __future__ import annotations

from opentelemetry import trace

tracer = trace.get_tracer("youtube_subs_opml")
