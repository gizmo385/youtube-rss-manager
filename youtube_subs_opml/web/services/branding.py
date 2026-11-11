"""The app's name, which each user may replace with their own.

The header, page titles and the web app manifest (the name an installed copy
gets on a home screen) all read it from here so they can't drift apart.
"""

from __future__ import annotations

from typing import Any

from ..models import User

DEFAULT_APP_NAME = "YouTube Subs OPML"
# Home screens truncate long labels, so the stock name has a short form.
DEFAULT_SHORT_NAME = "Subs"
MAX_APP_NAME = 40
# Roughly what fits under a home-screen icon before it's cut off.
_SHORT_NAME_LIMIT = 12

_ICONS: list[dict[str, str]] = [
    {"src": "/static/favicon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    # Listed separately: the icons are full-bleed, so they survive a launcher's
    # mask, and a combined "any maskable" entry is one some browsers skip.
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
]


def parse_app_name(raw: str | None) -> str:
    """A submitted name, tidied: whitespace collapsed, capped; "" for the default."""
    return " ".join((raw or "").split())[:MAX_APP_NAME].strip()


def app_name(user: User | None) -> str:
    return (user.app_name if user is not None else "") or DEFAULT_APP_NAME


def short_name(user: User | None) -> str:
    """The home-screen label: the user's name if they set one, else the short default."""
    custom = user.app_name if user is not None else ""
    return custom[:_SHORT_NAME_LIMIT].strip() if custom else DEFAULT_SHORT_NAME


def manifest(user: User | None) -> dict[str, Any]:
    from .themes import manifest_theme

    splash = manifest_theme(user.theme if user is not None else None)
    return {
        "id": "/watch",
        "name": app_name(user),
        "short_name": short_name(user),
        "description": "Subscription & archive manager",
        "start_url": "/watch",
        "scope": "/",
        "display": "standalone",
        # The installed app's splash and title bar follow the user's theme.
        "background_color": splash.bg,
        "theme_color": splash.panel,
        "icons": _ICONS,
    }
