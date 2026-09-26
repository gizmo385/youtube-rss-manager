FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # OpenTelemetry auto-instrumentation is wired in but exports nothing unless
    # the deployment points it at a collector (OTEL_TRACES_EXPORTER=otlp plus
    # OTEL_EXPORTER_OTLP_ENDPOINT).
    OTEL_TRACES_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none \
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    OTEL_PYTHON_FASTAPI_EXCLUDE_SPANS=receive,send

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY youtube_subs_opml/ ./youtube_subs_opml/

RUN uv pip install --system --no-cache ".[web]"

COPY alembic.ini ./
COPY alembic/ ./alembic/

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && opentelemetry-instrument uvicorn youtube_subs_opml.web.main:app --host 0.0.0.0 --port 8000"]
