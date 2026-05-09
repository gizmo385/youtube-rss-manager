# YouTube Subscriptions OPML Manager

A multi-user web app that syncs YouTube subscriptions via the YouTube Data API and exposes them as categorized OPML feeds. Designed for self-hosted setups where an RSS reader like FreshRSS pulls subscription feeds on a schedule.

A standalone CLI tool is also included for single-user, one-shot OPML export.

## Features

- OIDC login (works with any provider: Keycloak, Authentik, Authelia, etc.)
- Per-user YouTube OAuth with support for brand/managed channels
- Automatic subscription sync (every 6 hours) with manual trigger
- Organize channels into user-defined categories
- Ignore channels to exclude them from feeds
- Per-channel, per-category, and per-user shorts filtering (cascading preference: subscription > category > user)
- Token-authenticated OPML endpoints for RSS readers (`/opml/<token>/all.opml`, `/opml/<token>/<category-slug>.opml`)
- Rotatable OPML tokens
- Dark mode toggle
- Channel search and YouTube topic metadata display

## Prerequisites

- Docker and Docker Compose
- An OIDC provider (Keycloak, Authentik, Authelia, or any OpenID Connect-compatible identity provider)
- A Google Cloud project with the YouTube Data API v3 enabled and a Web application OAuth client

## Setup

### 1. External services

**OIDC provider:** Create an OIDC client (Authorization Code flow) in your identity provider. Set the valid redirect URI to `<BASE_URL>/auth/callback`. The provider must support OpenID Connect Discovery (a `/.well-known/openid-configuration` endpoint).

**Google Cloud:**

1. Go to Cloud Console > Credentials > Create OAuth client ID. Select **Web application** (not Desktop).
2. Set the authorized redirect URI to `<BASE_URL>/auth/youtube/callback`.
3. Enable the **YouTube Data API v3** on the same project.
4. The `youtube.readonly` scope is classified as "sensitive" by Google. In Testing mode, you must add users to the OAuth consent screen's test user list (max ~100). This is fine for household use.

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in the values:

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

### 3. Run

```bash
docker compose up -d
```

The app runs database migrations on startup automatically. It will be available on port 8000.

## Usage

1. Log in via your OIDC provider.
2. Go to **Settings** and connect a YouTube account. If you manage brand channels, Google will prompt you to choose which channel's subscriptions to link.
3. Trigger a sync from Settings (or wait for the automatic 6-hour sync).
4. Go to **Channels** to organize subscriptions into categories, ignore channels, or toggle shorts filtering.
5. Generate an OPML token from Settings. Add the feed URLs to your RSS reader.

### OPML feed URLs

- All non-ignored subscriptions: `/opml/<token>/all.opml`
- Single category: `/opml/<token>/<category-slug>.opml`

These endpoints are unauthenticated (the token acts as the credential) so RSS readers can fetch them directly.

### Shorts filtering

Each channel defaults to including shorts. This can be overridden at three levels, where the most specific non-null setting wins:

1. **Subscription** (per-channel) -- set from the Channels page
2. **Category** -- set from the Categories tab
3. **User default** -- set from the Settings page

When shorts are excluded for a channel, the OPML feed uses a YouTube playlist URL that serves only long-form videos.

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

## Notes

- Rotating `FERNET_KEY` invalidates all stored YouTube refresh tokens. Users will need to reconnect their YouTube accounts.
- YouTube API quota is 10,000 units/day. Subscription listing costs 1 unit per page (50 subs/page), so even thousands of subscriptions across users stay well within the free tier.
- FreshRSS supports only single-level OPML categories. The feed output is flat by design.
