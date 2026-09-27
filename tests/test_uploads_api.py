"""The Data API fallback's feed must look like YouTube's RSS to everything
downstream: the poller's entry parsing and the feed proxy's filtering."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from youtube_subs_opml.web.services import uploads_api
from youtube_subs_opml.web.services.poller import _parse_entries

CID = "UCOT2iLov0V7Re7ku_3UBtcQ"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"


def _item(video_id: str, *, public: bool = True, published: str | None = "2026-09-26T22:26:23Z") -> dict:
    snippet = {
        "publishedAt": "2026-09-26T23:00:00Z",
        "channelTitle": "Hank Green",
        "title": f"Video {video_id}",
        "description": "",
        "resourceId": {"kind": "youtube#video", "videoId": video_id},
    }
    if public:
        snippet["videoOwnerChannelId"] = CID
    details = {"videoId": video_id}
    if published:
        details["videoPublishedAt"] = published
    return {"snippet": snippet, "contentDetails": details}


def test_uploads_playlist_is_derived_from_the_channel_id():
    assert "playlistId=UUOT2iLov0V7Re7ku_3UBtcQ" in uploads_api.uploads_url(CID)
    assert "maxResults=15" in uploads_api.uploads_url(CID)


def test_feed_matches_the_rss_layout():
    root = ET.fromstring(uploads_api.build_feed(CID, [_item("lghuDPPiiE8")]))

    assert root.findtext(f"{_ATOM}title") == "Hank Green"
    assert root.findtext(f"{_ATOM}id") == "yt:channel:OT2iLov0V7Re7ku_3UBtcQ"
    entry = root.find(f"{_ATOM}entry")
    assert entry is not None
    # Same guid as the real feed, so readers don't duplicate the item.
    assert entry.findtext(f"{_ATOM}id") == "yt:video:lghuDPPiiE8"
    assert entry.findtext(f"{_YT}videoId") == "lghuDPPiiE8"
    assert entry.findtext(f"{_ATOM}published") == "2026-09-26T22:26:23+00:00"
    link = entry.find(f"{_ATOM}link")
    assert link is not None
    assert link.get("href") == "https://www.youtube.com/watch?v=lghuDPPiiE8"


def test_poller_parses_the_fallback_feed():
    xml = uploads_api.build_feed(CID, [_item("aaaaaaaaaaa"), _item("bbbbbbbbbbb", published=None)])

    entries = _parse_entries(xml)

    assert [e[0] for e in entries] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    first, second = entries[0][2], entries[1][2]
    assert first is not None and second is not None
    assert first.isoformat() == "2026-09-26T22:26:23+00:00"
    # Without videoPublishedAt, the playlist timestamp stands in.
    assert second.isoformat() == "2026-09-26T23:00:00+00:00"


def test_private_and_deleted_videos_are_skipped():
    xml = uploads_api.build_feed(CID, [_item("aaaaaaaaaaa"), _item("ccccccccccc", public=False)])

    assert [e[0] for e in _parse_entries(xml)] == ["aaaaaaaaaaa"]


def test_empty_channel_keeps_the_known_title():
    root = ET.fromstring(uploads_api.build_feed(CID, [], title="Hank Green"))

    assert root.findtext(f"{_ATOM}title") == "Hank Green"
    assert root.find(f"{_ATOM}entry") is None


def test_error_reason_prefers_the_specific_detail():
    import httpx

    body = {
        "error": {
            "status": "PERMISSION_DENIED",
            "errors": [{"reason": "forbidden"}],
            "details": [{"reason": "API_KEY_HTTP_REFERRER_BLOCKED"}],
        }
    }
    assert uploads_api.error_reason(httpx.Response(403, json=body)) == "API_KEY_HTTP_REFERRER_BLOCKED"
    quota = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    assert uploads_api.error_reason(httpx.Response(403, json=quota)) == "quotaExceeded"
    assert uploads_api.error_reason(httpx.Response(502, text="<html>")) == ""
