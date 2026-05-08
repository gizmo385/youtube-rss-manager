from __future__ import annotations

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_db
from .deps import get_current_user
from .models import User, YoutubeAccount
from .services.crypto import encrypt_token

oauth = OAuth()

router = APIRouter(prefix="/auth", tags=["auth"])


def register_oauth_clients(settings: Settings) -> None:
    oauth.register(
        name="oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=(
            f"{settings.oidc_issuer}/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )
    oauth.register(
        name="google",
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        server_metadata_url=(
            "https://accounts.google.com/.well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "https://www.googleapis.com/auth/youtube.readonly",
        },
    )


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    redirect_uri = f"{get_settings().base_url}/auth/callback"
    return await oauth.oidc.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    token = await oauth.oidc.authorize_access_token(request)
    userinfo = token.get("userinfo", {})

    sub = userinfo["sub"]
    email = userinfo.get("email", "")
    display_name = userinfo.get("name", userinfo.get("preferred_username", ""))

    user = db.execute(
        select(User).where(User.oidc_sub == sub)
    ).scalar_one_or_none()

    if user is None:
        user = User(oidc_sub=sub, email=email, display_name=display_name)
        db.add(user)
    else:
        user.email = email
        user.display_name = display_name

    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# --- YouTube per-user OAuth ---


@router.get("/youtube/connect")
async def youtube_connect(
    request: Request,
    user: User = Depends(get_current_user),
) -> RedirectResponse:
    redirect_uri = f"{get_settings().base_url}/auth/youtube/callback"
    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        access_type="offline",
        prompt="consent",
    )


@router.get("/youtube/callback")
async def youtube_callback(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    token = await oauth.google.authorize_access_token(request)

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise ValueError(
            "No refresh token returned. Google may not have issued one — "
            "try revoking access at myaccount.google.com and reconnecting."
        )

    access_token = token["access_token"]
    channel_id, channel_title = _get_authorized_channel(access_token)

    encrypted = encrypt_token(refresh_token)

    existing = db.execute(
        select(YoutubeAccount).where(
            YoutubeAccount.user_id == user.id,
            YoutubeAccount.channel_id == channel_id,
        )
    ).scalar_one_or_none()

    if existing is None:
        db.add(
            YoutubeAccount(
                user_id=user.id,
                channel_id=channel_id,
                channel_title=channel_title,
                refresh_token_encrypted=encrypted,
            )
        )
    else:
        existing.channel_title = channel_title
        existing.refresh_token_encrypted = encrypted

    db.commit()
    return RedirectResponse("/", status_code=303)


def _get_authorized_channel(access_token: str) -> tuple[str, str]:
    """Call YouTube API to get the channel ID and title for the authorized account."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(token=access_token)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError("No YouTube channel found for this account")
    ch = items[0]
    return ch["id"], ch["snippet"]["title"]
