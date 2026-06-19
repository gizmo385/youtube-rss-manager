"""Pure-logic tests for the Shorts feature — no DB, Keycloak, or network."""
from __future__ import annotations

import httpx

from youtube_subs_opml.opml import build_opml
from youtube_subs_opml.web.routes import feed
from youtube_subs_opml.web.services.shorts import (
    _probe_is_short,
    resolve_include_shorts,
)
from youtube_subs_opml.youtube import Subscription

CID = "UCabcdefghijklmnopqrstuv"

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
    xml = build_opml(
        [sub], proxy_base_url="http://h:8000", opml_token="TOK", category_slug="tech"
    )
    assert f"/feed/TOK/tech/{CID}.xml" in xml


def test_cli_fallback_uses_youtube_urls():
    on = Subscription(channel_id=CID, title="T", description="", include_shorts=True)
    off = Subscription(channel_id=CID, title="T", description="", include_shorts=False)
    assert "videos.xml?channel_id=" in build_opml([on])
    assert "UULF" in build_opml([off])


# --- cascade -------------------------------------------------------------

def test_cascade_precedence():
    assert resolve_include_shorts(False, True, True) is False   # subscription wins
    assert resolve_include_shorts(None, False, True) is False   # category wins
    assert resolve_include_shorts(None, None, True) is True     # user default
    assert resolve_include_shorts(None, None, False) is False


# --- feed filtering ------------------------------------------------------

def test_filter_drops_shorts(monkeypatch):
    monkeypatch.setattr(
        feed, "classify_videos",
        lambda ids, db: {"shortone111": True, "realvideo22": False},
    )
    out = feed._filter_shorts(SAMPLE_FEED, db=None).decode()
    assert "realvideo22" in out
    assert "shortone111" not in out


def test_filter_keeps_unknown(monkeypatch):
    """Fail open: a video with no verdict stays in the feed."""
    monkeypatch.setattr(feed, "classify_videos", lambda ids, db: {})
    out = feed._filter_shorts(SAMPLE_FEED, db=None).decode()
    assert "shortone111" in out and "realvideo22" in out


# --- probe ---------------------------------------------------------------

class _FakeResp:
    def __init__(self, status: int):
        self.status_code = status
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, status=None, raise_exc=False):
        self._status = status
        self._raise = raise_exc

    def stream(self, method, url, **kwargs):
        if self._raise:
            raise httpx.ConnectError("boom")
        return _FakeResp(self._status)


def test_probe_200_is_short():
    assert _probe_is_short("x", _FakeClient(status=200)) is True


def test_probe_redirect_is_not_short():
    assert _probe_is_short("x", _FakeClient(status=303)) is False


def test_probe_error_is_inconclusive():
    assert _probe_is_short("x", _FakeClient(raise_exc=True)) is None
