"""Jellyfin-compatible file layout and NFO metadata.

Each channel is a series and each video an episode of a TV Shows library. A
video is downloaded **once** to a canonical tree that no Jellyfin library scans,
then hardlinked into a per-user subtree that the user's own library does scan:

    {media_root}/.canonical/{Channel}/Season {YYYY}/          # download target
        {Channel} - S{YYYY}E{NN} - {Title}.mkv                # the real inode
        {Channel} - S{YYYY}E{NN} - {Title}.nfo
        {Channel} - S{YYYY}E{NN} - {Title}-thumb.jpg
    {media_root}/libraries/{user_id}/{Channel}/Season {YYYY}/ # per-user library
        {Channel} - S{YYYY}E{NN} - {Title}.mkv                # hardlink → inode

Series rather than Movies because it gives per-channel watched state and Next
Up. A per-user subtree (rather than one shared tree + playlists) gives each user
a Jellyfin library scoped to only their subscriptions — true per-user visibility
and independent watched state — while the hardlink keeps a single copy on disk.

The canonical tree is dot-prefixed and must **not** be added as a Jellyfin
library; each user's library points at ``libraries/{user_id}``. Pointing a
library at ``media_root`` itself would scan the canonical tree plus every user
tree and show N+1 copies of everything.

Episode numbers are derived from the video's upload date as ``MMDD`` (see
``date_episode_number``). Jellyfin sorts episodes within a season by this
number, so encoding the date makes episodes appear in chronological order
regardless of the order we happened to download them — the earlier scheme
numbered by download order, which put backfilled (newest-first) videos in
reverse. The number is a pure function of ``published_at``, so the basename is
stable and identical in every user's subtree.

Configure the library in Jellyfin with metadata downloading **off** — otherwise
it will cheerfully match your woodworking channel against a real TV series.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# The download target and the per-user library roots, both under media_root.
# The canonical dir is dot-prefixed as a second line of defence against it being
# picked up by a mis-configured library scan.
CANONICAL_SUBDIR = ".canonical"
LIBRARIES_SUBDIR = "libraries"

# Jellyfin series/season artwork filenames. The series poster and backdrop live
# in the channel folder; each season folder gets its own poster (reusing the
# channel avatar) so seasons don't render as blank tiles. Kept as a set so
# directory teardown can tell "only artwork left" from "still holding episodes".
POSTER_NAME = "poster.jpg"
BACKDROP_NAME = "backdrop.jpg"
ARTWORK_NAMES = frozenset({POSTER_NAME, BACKDROP_NAME})

# Windows-hostile characters plus anything that upsets path handling. Kept
# conservative because these files may be served over SMB.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_COMPONENT = 120


def sanitize(name: str) -> str:
    """Make a string safe as a single path component."""
    cleaned = _UNSAFE.sub("", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > _MAX_COMPONENT:
        cleaned = cleaned[:_MAX_COMPONENT].rstrip()
    return cleaned or "Untitled"


def episode_basename(
    channel_title: str, published_at: datetime | None, episode_number: int, title: str
) -> str:
    """``Channel - S2026E0315 - Title`` with no extension.

    The episode number is the ``MMDD`` of the upload date (``date_episode_number``),
    zero-padded to four digits so a March episode reads ``E0315``.
    """
    year = published_at.year if published_at else 1970
    return (
        f"{sanitize(channel_title)} - S{year:04d}E{episode_number:04d} - "
        f"{sanitize(title)}"
    )


def _season_subpath(channel_title: str, published_at: datetime | None) -> Path:
    year = published_at.year if published_at else 1970
    return Path(sanitize(channel_title)) / f"Season {year:04d}"


def canonical_channel_dir(media_root: str, channel_title: str) -> Path:
    """The channel (series) dir under the un-scanned ``.canonical`` tree.

    Holds the canonical ``poster.jpg``/``backdrop.jpg`` that every user's
    library hardlinks in, mirroring how episode files are shared by inode.
    """
    return Path(media_root) / CANONICAL_SUBDIR / sanitize(channel_title)


def canonical_episode_dir(
    media_root: str, channel_title: str, published_at: datetime | None
) -> Path:
    """The download target dir — under the un-scanned ``.canonical`` tree."""
    return (
        Path(media_root)
        / CANONICAL_SUBDIR
        / _season_subpath(channel_title, published_at)
    )


def user_episode_dir(
    media_root: str, user_id: int, channel_title: str, published_at: datetime | None
) -> Path:
    """The per-user library dir a canonical file is hardlinked into."""
    return (
        Path(media_root)
        / LIBRARIES_SUBDIR
        / str(user_id)
        / _season_subpath(channel_title, published_at)
    )


def media_is_ready(media_root: str, *, allow_unmounted: bool = False) -> bool:
    """True if ``media_root`` is a real place to store downloads.

    Guards against silently writing into the container's ephemeral filesystem
    when the media volume wasn't mounted: a bind/volume mount makes
    ``media_root`` a mount point, whereas a plain directory baked into the image
    is not. ``allow_unmounted`` is the escape hatch for local development, where
    the store is just a folder. Has no side effects — it never creates the dir.
    """
    p = Path(media_root)
    if not p.is_dir() or not os.access(p, os.W_OK):
        return False
    return allow_unmounted or os.path.ismount(p)


def episode_files(mkv_path: Path) -> list[Path]:
    """The ``.mkv`` plus its sidecars (``.nfo``, thumbnail) sharing its stem.

    Used to fan out or tear down all of an episode's files together. Matches
    only names where the character after the stem is ``.`` or ``-`` so a title
    that is a prefix of a longer one (``Foo`` vs ``Foo 2``) can't leak in;
    episode numbers are unique per channel-year, so this is belt-and-braces.
    """
    from glob import escape

    stem = mkv_path.stem
    out: list[Path] = []
    for p in sorted(mkv_path.parent.glob(escape(stem) + "*")):
        rest = p.name[len(stem):]
        if rest == "" or rest[0] in ".-":
            out.append(p)
    return out


def rmdir_if_stripped(d: Path) -> bool:
    """Remove ``d`` if it holds nothing but artwork sidecars (or is empty).

    A season/channel folder whose last episode was pruned still contains the
    ``poster.jpg``/``backdrop.jpg`` we dropped in, so a plain ``rmdir`` would
    fail and leave Jellyfin showing an empty series. This deletes those stray
    art files first, then removes the dir. Returns True if the dir is now gone,
    False if it still holds real content (episodes) and must be kept.
    """
    try:
        entries = list(d.iterdir())
    except FileNotFoundError:
        return True  # already gone — treat as removed so the climb continues
    if any(e.name not in ARTWORK_NAMES for e in entries):
        return False
    for e in entries:
        e.unlink()
    try:
        d.rmdir()
    except OSError:
        return False
    return True


def hardlink(src: Path, dst: Path) -> None:
    """Hardlink ``src`` to ``dst``, idempotently.

    Creating parent dirs as needed. If ``dst`` already exists pointing at the
    same inode we're done; if it points elsewhere (e.g. the canonical file was
    re-downloaded) it is replaced. Same-filesystem only — all paths live under
    one media mount, which is what makes the shared inode possible.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except FileExistsError:
        if dst.exists() and dst.samefile(src):
            return
        dst.unlink()
        os.link(src, dst)


def build_nfo(
    *,
    title: str,
    channel_title: str,
    published_at: datetime | None,
    episode_number: int,
    description: str,
    video_id: str,
) -> bytes:
    """Write our own episodedetails NFO rather than relying on a plugin.

    Everything here already lives in Postgres, so generating it directly is
    both more reliable than metadata-provider matching and fully under our
    control.
    """
    year = published_at.year if published_at else 1970
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "showtitle").text = channel_title
    ET.SubElement(root, "season").text = str(year)
    ET.SubElement(root, "episode").text = str(episode_number)
    ET.SubElement(root, "plot").text = description
    if published_at:
        ET.SubElement(root, "aired").text = published_at.date().isoformat()
    ET.SubElement(root, "uniqueid", type="youtube", default="true").text = video_id
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def date_episode_number(published_at: datetime | None) -> int:
    """Episode number encoding the upload date as ``MMDD`` (e.g. Mar 15 -> 315).

    Jellyfin orders episodes within a season by this number, so deriving it from
    the publish date — rather than the download order — makes videos appear
    oldest-first regardless of the order we fetched them. The season is the
    publish *year*, so ``MMDD`` is unique within a season except for two uploads
    on the same calendar day, a rare and harmless collision (both still play;
    their relative order is just unspecified).

    Falls back to ``0`` when the publish date is unknown — those land in the
    1970 season together, which is the best we can do without a date.
    """
    if published_at is None:
        return 0
    return published_at.month * 100 + published_at.day
