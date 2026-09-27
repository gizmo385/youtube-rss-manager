"""Pure-logic tests for the Shorts feature — no DB, Keycloak, or network."""

from __future__ import annotations

from typing import cast

import httpx
from sqlalchemy.orm import Session

from youtube_subs_opml.opml import build_opml
from youtube_subs_opml.web.routes import feed
from youtube_subs_opml.web.services.live import (
    _probe_live_status,
    resolve_include_live,
)
from youtube_subs_opml.web.services.shorts import (
    _probe_is_short,
    resolve_include_shorts,
)
from youtube_subs_opml.youtube import Subscription

CID = "UCabcdefghijklmnopqrstuv"

# The classifiers are monkeypatched in these tests, so the session is never used.
_NO_DB = cast(Session, None)

SAMPLE_FEED = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <title>Test channel</title>
  <entry><yt:videoId>shortone111</yt:videoId><title>a short</title></entry>
  <entry><yt:videoId>realvideo22</yt:videoId><title>a real one</title></entry>
</feed>"""


# --- OPML URL generation -------------------------------------------------


def test_url_is_stable_across_shorts_flag():
    """The feed URL must not change when the Shorts preference toggles."""
    on = Subscription(channel_id=CID, title="T", description="", include_shorts=True)
    off = Subscription(channel_id=CID, title="T", description="", include_shorts=False)
    u_on = build_opml([on], proxy_base_url="http://h:8000", opml_token="TOK")
    u_off = build_opml([off], proxy_base_url="http://h:8000", opml_token="TOK")
    assert u_on == u_off
    assert f"/feed/TOK/{CID}.xml" in u_on


def test_category_scoped_url():
    sub = Subscription(channel_id=CID, title="T", description="")
    xml = build_opml([sub], proxy_base_url="http://h:8000", opml_token="TOK", category_slug="tech")
    assert f"/feed/TOK/tech/{CID}.xml" in xml


def test_cli_fallback_uses_youtube_urls():
    on = Subscription(channel_id=CID, title="T", description="", include_shorts=True)
    off = Subscription(channel_id=CID, title="T", description="", include_shorts=False)
    assert "videos.xml?channel_id=" in build_opml([on])
    assert "UULF" in build_opml([off])


# --- cascade -------------------------------------------------------------


def test_cascade_precedence():
    assert resolve_include_shorts(False, True, True) is False  # subscription wins
    assert resolve_include_shorts(None, False, True) is False  # category wins
    assert resolve_include_shorts(None, None, True) is True  # user default
    assert resolve_include_shorts(None, None, False) is False


# --- feed filtering ------------------------------------------------------


def test_filter_drops_shorts(monkeypatch):
    monkeypatch.setattr(
        feed,
        "classify_videos",
        lambda ids, db: {"shortone111": True, "realvideo22": False},
    )
    out = feed._filter_feed(SAMPLE_FEED, db=_NO_DB, drop_shorts=True, drop_live=False).decode()
    assert "realvideo22" in out
    assert "shortone111" not in out


def test_filter_keeps_unknown(monkeypatch):
    """Fail open: a video with no verdict stays in the feed."""
    monkeypatch.setattr(feed, "classify_videos", lambda ids, db: {})
    out = feed._filter_feed(SAMPLE_FEED, db=_NO_DB, drop_shorts=True, drop_live=False).decode()
    assert "shortone111" in out and "realvideo22" in out


def test_filter_drops_live_and_upcoming(monkeypatch):
    monkeypatch.setattr(
        feed,
        "classify_live",
        lambda ids, db: {"shortone111": "upcoming", "realvideo22": "none"},
    )
    out = feed._filter_feed(SAMPLE_FEED, db=_NO_DB, drop_shorts=False, drop_live=True).decode()
    assert "realvideo22" in out  # status 'none' stays
    assert "shortone111" not in out  # 'upcoming' is dropped


# --- live cascade + probe ------------------------------------------------


def test_live_cascade_precedence():
    assert resolve_include_live(False, True, True) is False  # subscription wins
    assert resolve_include_live(None, False, True) is False  # category wins
    assert resolve_include_live(None, None, True) is True  # user default
    assert resolve_include_live(None, None, False) is False


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _page_client(text: str) -> httpx.Client:
    """A client whose every request returns ``text`` as a 200 page."""
    return _mock_client(lambda request: httpx.Response(200, text=text))


def test_probe_live_status_classifies():
    assert _probe_live_status("x", _page_client('"isUpcoming":true')) == "upcoming"
    assert _probe_live_status("x", _page_client('"isLiveNow":true')) == "live"
    assert _probe_live_status("x", _page_client('"isLiveContent":false')) == "none"


# --- probe ---------------------------------------------------------------


def _status_client(status: int) -> httpx.Client:
    """A client that answers every request with ``status``; 30x redirects to /watch."""
    headers = {"Location": "https://www.youtube.com/watch?v=x"} if 300 <= status < 400 else {}
    return _mock_client(lambda request: httpx.Response(status, headers=headers))


def _failing_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    return _mock_client(handler)


def test_probe_200_is_short():
    assert _probe_is_short("x", _status_client(200)) is True


def test_probe_redirect_is_not_short():
    assert _probe_is_short("x", _status_client(303)) is False


def test_probe_error_is_inconclusive():
    assert _probe_is_short("x", _failing_client()) is None
