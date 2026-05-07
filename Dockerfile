FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY youtube_subs_opml/ ./youtube_subs_opml/

RUN uv pip install --system --no-cache ".[web]"

COPY alembic.ini ./
COPY alembic/ ./alembic/

EXPOSE 8000

CMD ["uvicorn", "youtube_subs_opml.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
