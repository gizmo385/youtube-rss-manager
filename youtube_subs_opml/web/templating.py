from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent

templates = Jinja2Templates(directory=str(_HERE / "templates"))
# Jinja leaves ``globals`` unannotated, so type checkers infer it from the
# built-in defaults and reject anything else. Widen it for our additions.
_globals: dict[str, Any] = templates.env.globals


def _asset_version() -> str:
    """A cache-busting token for static assets: the newest mtime among them.

    Appended to static URLs so a redeploy (which rewrites the files)
    invalidates any copy cached by the browser or the Caddy layer in front,
    rather than serving stale CSS or JS against freshly-changed markup. The
    newest file rather than just style.css, so a JS-only change busts too.
    """
    try:
        return str(int(max(f.stat().st_mtime for f in (_HERE / "static").iterdir() if f.is_file())))
    except (OSError, ValueError):
        return "0"


# Computed once at import; the file doesn't change under a running process.
_globals["asset_v"] = _asset_version()

# Display labels shared across templates, defined once in the domain layer.
from .services.downloads import (  # noqa: E402 — after templates is defined
    SKIP_REASON_HELP,
    SKIP_REASON_LABELS,
    STATUS_LABELS,
)

_globals["STATUS_LABELS"] = STATUS_LABELS
_globals["SKIP_REASON_LABELS"] = SKIP_REASON_LABELS
_globals["SKIP_REASON_HELP"] = SKIP_REASON_HELP

from .services.prefs import LINK_TARGET_LABELS  # noqa: E402

_globals["LINK_TARGET_LABELS"] = LINK_TARGET_LABELS

from .services.branding import MAX_APP_NAME, app_name, short_name  # noqa: E402

_globals["app_name"] = app_name
_globals["app_short_name"] = short_name
_globals["MAX_APP_NAME"] = MAX_APP_NAME

from .services.themes import chrome_colors, parse_theme  # noqa: E402

_globals["theme_key"] = parse_theme
_globals["theme_chrome_colors"] = chrome_colors
