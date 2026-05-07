from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(...)
    base_url: str = Field("http://localhost:8000")
    session_secret: str = Field(...)
    fernet_key: str = Field(...)

    keycloak_issuer: str = Field("")
    keycloak_client_id: str = Field("")
    keycloak_client_secret: str = Field("")

    youtube_client_id: str = Field("")
    youtube_client_secret: str = Field("")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
