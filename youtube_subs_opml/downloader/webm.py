"""Archived videos as WebM where their codecs allow, so Firefox plays them.

yt-dlp merges into Matroska, and Firefox refuses any file whose header says
Matroska — whatever Content-Type it's served with — while playing the very
same streams once the header says WebM. WebM is a subset of Matroska (VP8,
VP9 or AV1 video; Opus or Vorbis audio; WebVTT subtitles), and that's what
YouTube serves for almost everything, so most archived files only need their
container rewritten: a stream copy, lossless and quick.

The file keeps its ``.mkv`` name: Firefox goes by the contents, and every
archived episode then has the same shape on disk, whichever container it
ended up in, for the naming, linking and pruning code. Anything WebM can't hold
(YouTube's occasional H.264) stays Matroska, and the in-page player falls back
to YouTube for it in Firefox — as it does for anything archived before this.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_WEBM_CODECS = {
    "video": {"vp8", "vp9", "av1"},
    "audio": {"opus", "vorbis"},
    "subtitle": {"webvtt"},
}


def doctype(path: Path) -> str | None:
    """The EBML DocType in ``path``'s header: 'matroska', 'webm', or None if
    it can't be read."""
    try:
        with path.open("rb") as f:
            head = f.read(64)
    except OSError:
        return None
    if not head.startswith(b"\x1a\x45\xdf\xa3"):  # not EBML at all
        return None
    at = head.find(b"\x42\x82")  # the DocType element
    if at < 0 or at + 3 > len(head) or not head[at + 2] & 0x80:
        return None  # absent, or a size wider than the one byte ffmpeg writes
    size = head[at + 2] & 0x7F
    return head[at + 3 : at + 3 + size].decode("ascii", "replace")


def fits_webm(path: Path) -> bool:
    """Whether every stream in ``path`` is one WebM can hold."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        streams = json.loads(result.stdout).get("streams", [])
    except (subprocess.TimeoutExpired, ValueError):
        return False
    return bool(streams) and all(s.get("codec_name") in _WEBM_CODECS.get(s.get("codec_type", ""), ()) for s in streams)


def convert(path: Path, *, timeout: int = 3600) -> bool:
    """Rewrite ``path`` in place as WebM if it's Matroska that fits. Returns
    whether it did.

    Run on a fresh download, before anything hardlinks it into a library:
    the new file replaces the old by rename, so other links would be left
    on the old one.
    """
    if doctype(path) != "matroska" or not fits_webm(path):
        return False
    # Hidden and not sharing the episode's stem, so a leftover from a crash
    # is never taken for one of its sidecars (naming.episode_files).
    part = path.with_name(f".{path.name}.webm-part")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "webm",
        str(part),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        result = None
    if result is None or result.returncode != 0:
        part.unlink(missing_ok=True)
        logger.warning("Couldn't rewrite %s as WebM: %s", path, result.stderr.strip()[:500] if result else "timed out")
        return False
    os.replace(part, path)
    return True
