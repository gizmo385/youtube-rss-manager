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
        if sub.include_shorts:
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
