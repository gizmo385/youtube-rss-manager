from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent

templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _asset_version() -> str:
    """A cache-busting token for static assets, from style.css's mtime.

    Appended to the stylesheet URL so a redeploy (which rewrites the file)
    invalidates any copy cached by the browser or the Caddy layer in front,
    rather than serving stale CSS against freshly-changed markup.
    """
    try:
        return str(int((_HERE / "static" / "style.css").stat().st_mtime))
    except OSError:
        return "0"


# Computed once at import; the file doesn't change under a running process.
templates.env.globals["asset_v"] = _asset_version()
