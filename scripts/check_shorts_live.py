#!/usr/bin/env python
"""Live sanity check for Shorts detection against real YouTube.

Fetches a channel's actual RSS feed and probes each video, printing which
entries are classified as Shorts. Hits the network; needs no DB or Keycloak.

Usage:
    uv run --extra web python scripts/check_shorts_live.py UC_xxxxxxxxxxxxxxxxxxxx
"""
from __future__ import annotations

import sys
from xml.etree import ElementTree as ET

import httpx

from youtube_subs_opml.opml import FEED_URL
from youtube_subs_opml.web.services.shorts import _USER_AGENT, _probe_is_short

_ATOM = "http://www.w3.org/2005/Atom"
_YT = "http://www.youtube.com/xml/schemas/2015"


def main(channel_id: str) -> int:
    feed = httpx.get(FEED_URL.format(channel_id=channel_id), follow_redirects=True)
    feed.raise_for_status()
    root = ET.fromstring(feed.content)

    with httpx.Client(headers={"User-Agent": _USER_AGENT}) as client:
        for entry in root.findall(f"{{{_ATOM}}}entry"):
            vid = entry.findtext(f"{{{_YT}}}videoId")
            title = entry.findtext(f"{{{_ATOM}}}title") or ""
            verdict = _probe_is_short(vid, client)
            label = {True: "SHORT ", False: "video ", None: "??????"}[verdict]
            print(f"[{label}] {vid}  {title[:70]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
