from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def _save(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)


def load_credentials(token_path: Path) -> Credentials | None:
    if not token_path.exists():
        return None
    return Credentials.from_authorized_user_file(str(token_path), SCOPES)


def get_credentials(token_path: Path) -> Credentials:
    """Return valid credentials, refreshing the access token if needed.

    Raises if no refresh token is available — caller should run the auth flow first.
    """
    creds = load_credentials(token_path)
    if creds is None:
        raise RuntimeError(
            f"No credentials at {token_path}. Run `youtube-subs-opml auth` first."
        )
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, token_path)
        return creds
    raise RuntimeError(
        f"Credentials at {token_path} are invalid and cannot be refreshed. "
        "Run `youtube-subs-opml auth` again."
    )


def run_auth_flow(client_secrets_path: Path, token_path: Path) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save(creds, token_path)
    return creds
