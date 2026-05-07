# YouTube Subscriptions OPML — project context

## What this is
A multi-user web app for the operator's home server. Each user logs in via Keycloak (SSO), connects their YouTube/brand account via Google OAuth, then organizes their subscriptions into categories and ignores noise. Each category is exposed as a per-user OPML feed at an unguessable URL that FreshRSS (or any OPML reader) pulls on schedule.

A single-user CLI (`youtube-subs-opml` console script under `youtube_subs_opml/cli.py`) lives alongside as the original tool — it predates the web app and is kept for one-shot personal use. The web app reuses `youtube_subs_opml/youtube.py` and `opml.py` as its engine.

## Why these stack choices
- **FastAPI + Jinja + HTMX, sync SQLAlchemy 2.0, Postgres 16, Alembic, docker-compose** — matches operator's home-server conventions. No SPA build pipeline.
- **Authlib** for both Keycloak (human SSO) and Google (per-user YouTube) — one OAuth library covers both.
- **APScheduler in-process** for periodic sub refresh — home-server scale, no separate worker needed.
- **Fernet** symmetric encryption for refresh-token at-rest (`FERNET_KEY` env, generated via `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
- **OPML URLs are token-in-path** (`/opml/<token>/<category-slug>.opml`), unauthenticated. FreshRSS fetches without a Keycloak session. Token is per-user and rotatable from settings.

## Phase plan
- [x] Phase 1: FastAPI skeleton + docker-compose foundation
- [x] Phase 2: SQLAlchemy models + Alembic initial migration (verified up/down)
- [ ] Phase 3: Keycloak OIDC login (Authlib, session middleware, current-user dependency, upsert User on first login)
- [ ] Phase 4: YouTube per-user OAuth (web flow), encrypt and store refresh token, capture authorized channel id, link `YoutubeAccount` to current user
- [ ] Phase 5: Subscription sync service — manual trigger endpoint first, scheduled later. Fetches subs, upserts `Channel` rows, reconciles `Subscription` rows.
- [ ] Phase 6: Bulk-assign UI — channels page with multi-select + assign-to-category / ignore actions, HTMX-driven, uncategorized view
- [ ] Phase 7: Category CRUD (per-user, name + auto slug)
- [ ] Phase 8: Token-based public OPML endpoints (one per category + an "all" view)

## External setup the operator must do (not code, not auto-doable)
1. **Google Cloud OAuth Web client.** Cloud Console → Credentials → OAuth client ID → type **Web application** (not Desktop — the CLI's Desktop client does NOT work for the web flow; these are separate OAuth clients on the same project). Authorized redirect URI: `${BASE_URL}/auth/youtube/callback`.
2. **Enable YouTube Data API v3** on the same Cloud project.
3. **OAuth consent screen test users.** `youtube.readonly` is a Google "sensitive" scope; in Testing mode the consent screen caps at ~100 test users. Household scale is fine. Going beyond requires Google's app verification (privacy policy, video walkthrough — weeks).
4. **Keycloak realm + client.** OIDC, Authorization Code flow. Valid redirect URI `${BASE_URL}/auth/callback`. **Open decision:** confidential client (with secret) vs public client + PKCE — Phase 3 code will be aligned to whichever the operator picks.

## Non-obvious constraints
- **YouTube brand accounts:** during Google OAuth consent, after the Google login picker, Google shows a separate "Choose an account" picker letting the user pick personal channel vs any brand channel they manage. The resulting refresh token is bound to that channel only; `subscriptions.list?mine=true` returns subs of the chosen channel, not personal+brand combined. Phase 4 UI must communicate this so users pick correctly.
- **FreshRSS supports only single-level OPML categories.** Nested sub-categories don't import. So OPML output stays flat: one `<outline>` folder per category, each containing `<outline type="rss">` feeds. Mixed-level layouts hit a bug fixed in FreshRSS 1.18.0 — assume modern FreshRSS.
- **YouTube API quota:** 10,000 units/day free. `subscriptions.list` is 1 unit per page (50 subs/page). Thousands of subs across all users still ≪ quota. No billing required.
- **Refresh tokens are sensitive.** Stored as `LargeBinary` after Fernet encryption. Never log them. Rotating `FERNET_KEY` invalidates all stored tokens — users would need to re-auth.

## Schema summary (current)
- `users` (id, keycloak_sub unique, email, display_name)
- `youtube_accounts` (user_id FK, channel_id, channel_title, refresh_token_encrypted bytes, last_synced_at) — one user can connect multiple brand accounts
- `channels` (channel_id PK, title, description, last_seen_at) — shared across users
- `subscriptions` (user_id, channel_id PK composite, account_id FK, ignored bool) — `ignored` is a simple boolean, not a separate table
- `categories` (id, user_id FK, name, slug) — per-user; `(user_id, slug)` unique
- `channel_categories` (user_id, channel_id, category_id PK composite) — channels can be in multiple categories per user
- `opml_tokens` (user_id unique, token unique) — rotatable, one per user

## Dev quick reference
```bash
cp .env.example .env                                      # then fill secrets (.env.example has generation hints)
docker compose up -d db                                   # Postgres on 127.0.0.1:5432
uv sync --extra web                                       # install with web deps
uv run --extra web alembic upgrade head                   # apply migrations (DATABASE_URL must point at the db)
uv run --extra web uvicorn youtube_subs_opml.web.main:app --reload   # run app locally
docker compose up                                         # full stack (app + db)
```

For local migration runs, `DATABASE_URL` must be set explicitly (the `.env` is consumed by the app at runtime, not by alembic-from-the-shell). Example:
```bash
DATABASE_URL=postgresql+psycopg://yts:<password>@localhost:5432/yts uv run --extra web alembic upgrade head
```

## Repo layout
- `youtube_subs_opml/` — core engine (CLI). `oauth.py` (loopback OAuth for the CLI), `youtube.py` (subs fetcher), `opml.py` (OPML builder), `cli.py` (CLI entrypoint).
- `youtube_subs_opml/web/` — FastAPI app. `main.py`, `config.py`, `db.py`, `models.py`. Phases 3–8 will add `auth/`, `routes/`, `services/`, `templates/`, `static/`.
- `alembic/` — migrations. Initial migration creates 7 tables.
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `pyproject.toml`, `uv.lock`.
