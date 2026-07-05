# YouTube Subscriptions OPML Manager

A multi-user web app that syncs YouTube subscriptions via the YouTube Data API and exposes them as categorized OPML feeds. Designed for self-hosted setups where an RSS reader like FreshRSS pulls subscription feeds on a schedule.

![YouTube Kanban Board](./images/opml_kanban_board.png)

## Features

- OIDC login for providers such as Keycloak, Authentik, Authelia, etc.
- Per-user YouTube OAuth
- Automatic subscription sync (every 6 hours)
- Organize channels into user-defined categories
- Per-channel, per-category, and per-user shorts filtering (cascading preference: subscription > category > user)
- Token-authenticated OPML endpoints for RSS readers (`/opml/<token>/all.opml`, `/opml/<token>/<category-slug>.opml`)

![Channel list](./images/opml_channels_list.png)

![Category management](./images/opml_categories_view.png)

## Prerequisites

- Docker and Docker Compose
- A Google Cloud project with the YouTube Data API v3 enabled and a Web application OAuth client

Authentication can either be managed via:
- An OIDC provider (Keycloak, Authentik, Authelia, or any OpenID Connect-compatible identity provider)
- A single local user, enabled with the `LOCAL_MODE` environment variable

## Setup

### 1. External services

**OIDC provider:**
1. Create an OIDC client (Authorization Code flow) in your identity provider.
2. Set the valid redirect URI to `<BASE_URL>/auth/callback`.
3. The provider must support OpenID Connect Discovery (a `/.well-known/openid-configuration` endpoint).

**Google Cloud:**

1. Go to Cloud Console > Credentials > Create OAuth client ID. Select **Web application** (not Desktop).
2. Set the authorized redirect URI to `<BASE_URL>/auth/youtube/callback`.
3. Enable the **YouTube Data API v3** on the same project.
4. The `youtube.readonly` scope is classified as "sensitive" by Google. In Testing mode, you must add users to the OAuth consent screen's test user list (max ~100). This is fine for household use.

### 2. Configure environment

```bash
cp .env.example .env
```

If you're using auth via an OIDC provider, fill in the values:

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` | Database password |
| `SESSION_SECRET` | Random string for cookie signing. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `FERNET_KEY` | Encryption key for stored refresh tokens. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `BASE_URL` | Public URL of the app (e.g. `https://youtube-rss.example.com`) |
| `OIDC_ISSUER` | OIDC issuer URL (e.g. `https://sso.example.com/realms/myrealm`) |
| `OIDC_CLIENT_ID` | OIDC client ID |
| `OIDC_CLIENT_SECRET` | OIDC client secret |
| `YOUTUBE_CLIENT_ID` | Google OAuth client ID |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth client secret |


If you're only planning on using the local setup and don't require OIDC or YouTube client support,
then just set `LOCAL_MODE=1` in your environment config.

### 3. Run

```bash
docker compose up -d
```

The app runs database migrations on startup automatically. It will be available on port 8000.

## Local development

```bash
docker compose up -d db
uv sync --extra web
uv run --extra web alembic upgrade head
uv run --extra web uvicorn youtube_subs_opml.web.main:app --reload
```

`DATABASE_URL` must be set for Alembic when running outside Docker:

```bash
DATABASE_URL=postgresql+psycopg://yts:<password>@localhost:5432/yts uv run --extra web alembic upgrade head
```

## CLI

A standalone CLI tool is available for one-shot OPML export without the web app:

```bash
uv sync
uv run youtube-subs-opml
```

This uses a separate Desktop OAuth flow and writes OPML to stdout.
