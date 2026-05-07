from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="YouTube Subscriptions OPML")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> dict[str, str]:
    return {"app": "youtube-subs-opml", "status": "running"}
