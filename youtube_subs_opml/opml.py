from __future__ import annotations

from datetime import datetime, timezone
from xml.dom import minidom
from xml.etree import ElementTree as ET

from .youtube import Subscription

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHANNEL_URL = "https://www.youtube.com/channel/{channel_id}"


def build_opml(
    subscriptions: list[Subscription],
    title: str = "YouTube Subscriptions",
) -> str:
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    body = ET.SubElement(opml, "body")
    folder = ET.SubElement(body, "outline", text=title, title=title)
    for sub in sorted(subscriptions, key=lambda s: s.title.lower()):
        ET.SubElement(
            folder,
            "outline",
            attrib={
                "type": "rss",
                "text": sub.title,
                "title": sub.title,
                "xmlUrl": FEED_URL.format(channel_id=sub.channel_id),
                "htmlUrl": CHANNEL_URL.format(channel_id=sub.channel_id),
            },
        )
    rough = ET.tostring(opml, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
