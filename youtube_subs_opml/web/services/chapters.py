"""A video's chapters, for the in-page player: the creator's own sections,
not SponsorBlock's (see services.sponsorblock for those).

Two sources, best first:

- What yt-dlp found when the downloader probed the video (``Video.chapters``).
  That's YouTube's own list, so it includes the auto-generated chapters too,
  but only archived videos get probed: once when archived, and once more a
  few hours after upload if that first look came too soon to find any
  (downloader.worker.recheck_chapters).
- Timestamps in the description, read the way YouTube reads them: a list that
  starts at 0:00, has at least three entries in order, each at least ten
  seconds long. That's free and covers every video whose description we have,
  from the poller's RSS fetch, the history backfill or the probe.

Nothing here asks YouTube for anything: a request per video played would be
slow and, from a server's address, quick to get throttled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Video

#: YouTube's rules for a description's chapter list.
MIN_CHAPTERS = 3
MIN_LENGTH = 10

# h:mm:ss, mm:ss or m:ss, not inside a longer number or another timestamp.
_TIMESTAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
# What surrounds a timestamp in a list: bullets, brackets and separators.
_EDGE = " \t-–—:|•·*>[](){}"


@dataclass(frozen=True)
class Chapter:
    start: float
    title: str
    end: float | None = None


def parse_description(text: str | None) -> list[Chapter]:
    """The chapter list in a video description, or [] if it hasn't one YouTube
    would show. Only the start of each is known; the next one's start (or
    the end of the video) is where it ends."""
    found: list[Chapter] = []
    for line in (text or "").splitlines():
        m = _TIMESTAMP.search(line)
        if m is None:
            continue
        hours, minutes, seconds = m.group(1), int(m.group(2)), int(m.group(3))
        if seconds >= 60 or (hours is not None and minutes >= 60):
            continue
        start = int(hours or 0) * 3600 + minutes * 60 + seconds
        title = (line[: m.start()].strip(_EDGE) + " " + line[m.end() :].strip(_EDGE)).strip()
        if start == 0:
            # A list starts at 0:00; one found earlier was something else (a
            # "skip to 4:20" note, say), so start over from here.
            found = [Chapter(0, title)]
        elif found and start > found[-1].start:
            found.append(Chapter(start, title))
        elif found:
            # Out of order: the list is over (the rest is likely a tracklist
            # or a comment's timestamps). Keep what came before.
            break
    if len(found) < MIN_CHAPTERS:
        return []
    if any(b.start - a.start < MIN_LENGTH for a, b in pairwise(found)):
        return []
    return found


def from_ytdlp(raw: list[dict] | None) -> list[Chapter]:
    """yt-dlp's ``chapters`` field (``start_time``, ``end_time``, ``title``),
    or the same as stored in ``Video.chapters``."""
    out: list[Chapter] = []
    for c in raw or []:
        start = c.get("start_time", c.get("start"))
        if not isinstance(start, int | float):
            continue
        end = c.get("end_time", c.get("end"))
        end = float(end) if isinstance(end, int | float) else None
        out.append(Chapter(float(start), str(c.get("title") or ""), end))
    return out


def to_json(chapters: list[Chapter]) -> list[dict]:
    return [{"start": c.start, "end": c.end, "title": c.title} for c in chapters]


def for_video(db: Session, video_id: str) -> list[Chapter]:
    """The video's chapters from the best source there is, or []."""
    row = db.execute(select(Video.chapters, Video.description).where(Video.video_id == video_id)).one_or_none()
    if row is None:
        return []
    stored, description = row
    return from_ytdlp(stored) or parse_description(description)
