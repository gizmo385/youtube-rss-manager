"""yt-dlp wrapper: metadata probe and download.

The probe exists so a six-hour stream is rejected before a single byte of media
is fetched. ``--dump-json --skip-download`` hits the same extraction path the
real download would, so a successful probe is also a decent signal the download
will work.

The YouTube Data API is deliberately not used for duration: it costs quota, and
the RSS feed doesn't carry it. yt-dlp is already a dependency here.

Note that the app's existing YouTube OAuth tokens are useless to yt-dlp — a
completely different auth mechanism. Don't try to reuse them. If cookies become
necessary for age-gated or members-only content, use a burner Google account;
cookies lifted from a self-hosted box are a real account-compromise risk.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


class ProbeError(RuntimeError):
    """Metadata could not be retrieved (private, deleted, geo-blocked, ...)."""


class DownloadError(RuntimeError):
    """Download failed. May be transient — the worker decides whether to retry."""


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: int | None
    title: str
    description: str
    uploader: str
    thumbnail_url: str | None


@dataclass(frozen=True)
class ChannelArt:
    avatar_url: str | None
    banner_url: str | None


def _pick_thumbnail(thumbnails: list[dict], keyword: str) -> str | None:
    """Highest-resolution thumbnail whose id/url mentions ``keyword``.

    yt-dlp tags a channel's images by role — ``avatar_uncropped``,
    ``banner_uncropped`` — so we match on the keyword and then prefer the
    largest by pixel area (``preference``/``height`` aren't always present).
    """
    matches = [
        t for t in thumbnails
        if keyword in str(t.get("id", "")).lower()
        or keyword in str(t.get("url", "")).lower()
    ]
    if not matches:
        return None
    best = max(matches, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    return best.get("url")


def probe_channel(channel_id: str, *, timeout: int = 60) -> ChannelArt:
    """Fetch a channel's avatar and banner URLs without a full video listing.

    The channel-level ``thumbnails`` we want live on the playlist object, so
    ``--flat-playlist --playlist-items 1`` grabs them while stopping after the
    first entry rather than paginating the whole channel — a single cheap
    request. The one listed entry is ignored.
    """
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--flat-playlist",
        "--playlist-items", "1",
        "--no-warnings",
        f"https://www.youtube.com/channel/{channel_id}",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"channel probe timed out for {channel_id}") from exc

    if result.returncode != 0:
        raise ProbeError(
            result.stderr.strip()[:2000] or f"channel probe failed for {channel_id}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"unparseable channel probe for {channel_id}") from exc

    thumbnails = data.get("thumbnails") or []
    avatar = _pick_thumbnail(thumbnails, "avatar")
    banner = _pick_thumbnail(thumbnails, "banner")
    # Fall back to the plain ``thumbnail`` field if the roles weren't tagged.
    if avatar is None:
        avatar = data.get("thumbnail")
    return ChannelArt(avatar_url=avatar, banner_url=banner)


def probe(video_id: str, *, timeout: int = 120) -> VideoMetadata:
    """Fetch metadata without downloading media."""
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        WATCH_URL.format(video_id=video_id),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"probe timed out for {video_id}") from exc

    if result.returncode != 0:
        raise ProbeError(result.stderr.strip()[:2000] or f"probe failed for {video_id}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"unparseable probe output for {video_id}") from exc

    duration = data.get("duration")
    return VideoMetadata(
        duration_seconds=int(duration) if duration is not None else None,
        title=data.get("title") or "",
        description=data.get("description") or "",
        uploader=data.get("uploader") or "",
        thumbnail_url=data.get("thumbnail"),
    )


def download(
    video_id: str,
    output_path: Path,
    *,
    video_format: str,
    sleep_interval: int = 5,
    max_retries: int = 3,
    timeout: int = 7200,
    write_thumbnail: bool = True,
) -> Path:
    """Download to ``output_path`` (extension supplied by the merge format).

    Returns the actual path written. Raises DownloadError on failure.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "-f", video_format,
        "--merge-output-format", "mkv",
        "--no-warnings",
        "--no-playlist",
        "--sleep-requests", "2",
        "--sleep-interval", str(sleep_interval),
        "--retries", str(max_retries),
        "-o", str(output_path.with_suffix(".%(ext)s")),
    ]
    if write_thumbnail:
        # Jellyfin only recognises an episode image named ``<video>-thumb.jpg``;
        # the default template would write ``<video>.jpg``, which it ignores. A
        # per-type output template (``thumbnail:``) names it correctly without
        # touching the media file's own template above.
        cmd += [
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "-o", f"thumbnail:{output_path}-thumb.%(ext)s",
        ]
    cmd.append(WATCH_URL.format(video_id=video_id))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"download timed out for {video_id}") from exc

    if result.returncode != 0:
        raise DownloadError(result.stderr.strip()[:2000] or "download failed")

    produced = output_path.with_suffix(".mkv")
    if not produced.exists():
        candidates = sorted(output_path.parent.glob(output_path.stem + ".*"))
        media = [c for c in candidates if c.suffix not in (".jpg", ".webp", ".nfo")]
        if not media:
            raise DownloadError(f"no output file produced for {video_id}")
        produced = media[0]
    return produced


def extract_audio(
    video_id: str,
    output_path: Path,
    *,
    sleep_interval: int = 5,
    timeout: int = 3600,
) -> Path:
    """Download audio only, for podcast feeds.

    Much cheaper on disk than the video, which is what makes podcast generation
    attractive as a separate output rather than a byproduct.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "--extract-audio",
        "--audio-format", "m4a",
        "--no-warnings",
        "--no-playlist",
        "--sleep-interval", str(sleep_interval),
        "-o", str(output_path.with_suffix(".%(ext)s")),
        WATCH_URL.format(video_id=video_id),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"audio extraction timed out for {video_id}") from exc

    if result.returncode != 0:
        raise DownloadError(result.stderr.strip()[:2000] or "audio extraction failed")

    produced = output_path.with_suffix(".m4a")
    if not produced.exists():
        raise DownloadError(f"no audio file produced for {video_id}")
    return produced
