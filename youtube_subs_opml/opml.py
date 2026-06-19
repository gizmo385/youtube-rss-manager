from __future__ import annotations

from datetime import datetime, timezone
from xml.dom import minidom
from xml.etree import ElementTree as ET

from .youtube import Subscription

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
FEED_URL_NO_SHORTS = "https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{channel_id_suffix}"
CHANNEL_URL = "https://www.youtube.com/channel/{channel_id}"


def build_opml(
    subscriptions: list[Subscription],
    title: str = "YouTube Subscriptions",
    *,
    proxy_base_url: str | None = None,
    opml_token: str | None = None,
    category_slug: str | None = None,
) -> str:
    """Build OPML for the given subscriptions.

    When ``proxy_base_url`` and ``opml_token`` are supplied (the web app),
    *every* channel's ``xmlUrl`` points at our feed proxy, regardless of its
    Shorts preference. This keeps the URL stable when the preference is
    toggled — the proxy decides at request time whether to filter Shorts.
    ``category_slug`` scopes the URL so the proxy can resolve the
    subscription > category > user cascade for category feeds.

    Without proxy params (the CLI, which has no server), it emits YouTube's own
    feed URLs: the plain feed for shorts-included channels, the ``UULF``
    playlist for shorts-excluded ones.
    """
    proxy_base = proxy_base_url.rstrip("/") if proxy_base_url else None
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    body = ET.SubElement(opml, "body")
    folder = ET.SubElement(body, "outline", text=title, title=title)
    for sub in sorted(subscriptions, key=lambda s: s.title.lower()):
        if proxy_base and opml_token:
            # Always route through our middleman so the URL never depends on
            # the Shorts preference; the proxy fetches the real feed and
            # filters Shorts when needed (the UULF trick is broken upstream).
            if category_slug:
                xml_url = (
                    f"{proxy_base}/feed/{opml_token}/{category_slug}"
                    f"/{sub.channel_id}.xml"
                )
            else:
                xml_url = f"{proxy_base}/feed/{opml_token}/{sub.channel_id}.xml"
        elif sub.include_shorts:
            xml_url = FEED_URL.format(channel_id=sub.channel_id)
        else:
            # Strip "UC" prefix and use UULF playlist to exclude Shorts
            xml_url = FEED_URL_NO_SHORTS.format(
                channel_id_suffix=sub.channel_id[2:]
            )
        ET.SubElement(
            folder,
            "outline",
            attrib={
                "type": "rss",
                "text": sub.title,
                "title": sub.title,
                "xmlUrl": xml_url,
                "htmlUrl": CHANNEL_URL.format(channel_id=sub.channel_id),
            },
        )
    rough = ET.tostring(opml, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
