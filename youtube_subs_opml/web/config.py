from __future__ import annotations

import base64
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable, deterministic local secrets so encryption survives restarts locally.
# Never used in production — only filled in when LOCAL_MODE is set.
_LOCAL_SESSION_SECRET = "local-insecure-session-secret-do-not-use-in-prod"
_LOCAL_FERNET_KEY = base64.urlsafe_b64encode(
    b"local-fernet-key-not-secure-0000"
).decode()
_LOCAL_DATABASE_URL = "sqlite:///./local.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # When true, bypass Keycloak auth and fill in ephemeral defaults for the
    # secrets/database below so the app runs locally with no external services.
    local_mode: bool = Field(False)
    local_user_email: str = Field("local@example.com")

    database_url: str = Field("")
    base_url: str = Field("http://localhost:8000")
    session_secret: str = Field("")
    fernet_key: str = Field("")

    oidc_issuer: str = Field("")
    oidc_client_id: str = Field("")
    oidc_client_secret: str = Field("")

    youtube_client_id: str = Field("")
    youtube_client_secret: str = Field("")

    # --- Video archive ------------------------------------------------------
    # Root of the Jellyfin-visible media tree. Must be the same path inside the
    # downloader container and (via the same bind mount) inside Jellyfin.
    media_root: str = Field("/media/youtube")
    # Refuse to download unless media_root is a real mount, so a forgotten
    # volume doesn't silently fill the container's ephemeral layer. Set True for
    # local development, where the store is just a folder (see local-mode
    # defaults below).
    allow_unmounted_media: bool = Field(False)
    # How often the poller re-reads channel RSS. YouTube's feed only carries
    # the ~15 most recent entries, so this bounds what can be captured for
    # prolific channels — 6 hours would silently lose videos.
    poll_interval_minutes: int = Field(20)
    # Keep this at 1. Concurrent downloads from one IP are the fastest route to
    # "Sign in to confirm you're not a bot".
    download_concurrency: int = Field(1)
    ytdlp_format: str = Field("bestvideo[height<=1080]+bestaudio/best[height<=1080]")
    # Politeness knobs passed through to yt-dlp.
    ytdlp_sleep_interval: int = Field(5)
    ytdlp_max_retries: int = Field(3)
    # How often to resolve Jellyfin item ids and reconcile playlists. HTTP-only,
    # runs in the web process. A no-op for users without a Jellyfin account.
    jellyfin_sync_interval_minutes: int = Field(30)
    # --- Podcast feeds ------------------------------------------------------
    # RSS <language> for generated podcast feeds.
    podcast_language: str = Field("en")
    # Optional channel-level artwork. Apple requires a square image ≥1400px over
    # HTTPS for *directory submission*; "Add a Show by URL" is lenient, so this
    # is optional. When unset, no <itunes:image> is emitted. Point it at a
    # public URL you control (e.g. a static asset served by this app).
    podcast_cover_url: str = Field("")

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> "Settings":
        if self.local_mode:
            self.database_url = self.database_url or _LOCAL_DATABASE_URL
            self.session_secret = self.session_secret or _LOCAL_SESSION_SECRET
            self.fernet_key = self.fernet_key or _LOCAL_FERNET_KEY
            # Local media is a plain folder, not a mount.
            self.allow_unmounted_media = True
            return self

        missing = [
            name
            for name in ("database_url", "session_secret", "fernet_key")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(
                f"Missing required settings: {', '.join(missing)}. "
                "Set them, or enable LOCAL_MODE for local defaults."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
