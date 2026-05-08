from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import register_oauth_clients, router as auth_router
from .config import get_settings
from .deps import get_optional_user
from .models import User
from .templating import templates
from .routes.categories import router as categories_router
from .routes.channels import router as channels_router
from .routes.opml import router as opml_router
from .routes.settings import router as settings_router
from .services.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    register_oauth_clients(settings)
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="YouTube Subscriptions OPML", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=get_settings().session_secret, max_age=86400 * 14
)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
app.include_router(auth_router)
app.include_router(categories_router)
app.include_router(channels_router)
app.include_router(opml_router)
app.include_router(settings_router)


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: Exception) -> JSONResponse | RedirectResponse:
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse("/auth/login", status_code=303)
    return JSONResponse(status_code=401, content={"detail": "Not authenticated"})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_model=None)
def index(
    request: Request,
    user: User | None = Depends(get_optional_user),
):
    if user is not None:
        return RedirectResponse("/channels", status_code=303)
    return templates.TemplateResponse(request, "login.html", context={"user": None})
