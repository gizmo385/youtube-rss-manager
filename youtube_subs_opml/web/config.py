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

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> "Settings":
        if self.local_mode:
            self.database_url = self.database_url or _LOCAL_DATABASE_URL
            self.session_secret = self.session_secret or _LOCAL_SESSION_SECRET
            self.fernet_key = self.fernet_key or _LOCAL_FERNET_KEY
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
