"""SponsorBlock cuts of podcast audio.

A subscriber with ``podcast_sponsorblock`` set to ``remove`` gets episodes
with the segment kinds they chose cut out. The extracted audio is shared by
everyone with the channel as a podcast, so it's left alone: each distinct
choice of kinds gets its own cut copy under ``{media_root}/.podcast-cuts/``,
outside the library tree so nothing hardlinks, renumbers or scans it, and the
feeds serve whichever copy matches the subscriber (see
``web/services/sponsorblock.podcast_cut``).

Segments are crowd-sourced, and a fresh upload usually has none until people
have watched it — often hours later. So an episode is checked when its audio
first appears and then again every ``RECHECK_EVERY`` until it's
``RECHECK_FOR`` old, and re-cut whenever what there is to cut has changed.
A podcast app that already downloaded the earlier version keeps it; anything
fetched afterwards gets the new one.

Cutting copies the audio around the segments rather than re-encoding it, as
yt-dlp's ``--sponsorblock-remove`` does: next to no CPU, and every AAC frame
(~23ms) is a place it can cut, so the cuts are as good as exact. The cut copy
is written
under a new name each time, so a podcast app partway through downloading the
previous one isn't served a file that changed underneath it.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..web.models import AudioCut, Download, Video
from ..web.services import sponsorblock
from ..web.services.archive import wanted_audio_cuts

logger = logging.getLogger(__name__)

CUTS_SUBDIR = ".podcast-cuts"

RECHECK_EVERY = timedelta(hours=6)
RECHECK_FOR = timedelta(days=7)

# Segments closer together than this are cut as one: the sliver between them
# isn't worth keeping.
_MERGE_GAP = 1.0

# Each pass does a bounded amount, so the worker gets back to downloading.
_MAX_LOOKUPS = 50
_MAX_CUTS = 3

Range = tuple[float, float]


class CutError(RuntimeError):
    """ffmpeg couldn't produce the cut copy."""


def plan(segments: list[sponsorblock.Segment], kinds: list[str], duration: int | None) -> list[Range]:
    """The ranges to cut: the segments of ``kinds``, clipped to the episode
    and merged where they overlap or nearly touch."""
    ranges: list[Range] = []
    for seg in sorted((s for s in segments if s.category in kinds), key=lambda s: s.start):
        start, end = max(0.0, seg.start), seg.end if duration is None else min(seg.end, float(duration))
        if end <= start:
            continue
        if ranges and start <= ranges[-1][1] + _MERGE_GAP:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def fingerprint(ranges: list[Range]) -> str:
    """``ranges`` as stored in ``AudioCut.segments``: to the tenth of a
    second, so a re-vote nudging a boundary by a few milliseconds doesn't
    count as a change."""
    return ",".join(f"{s:.1f}-{e:.1f}" for s, e in ranges)


def cut_path(media_root: str, video_id: str, kinds: str, segments: str) -> Path:
    digest = hashlib.sha256(f"{kinds}|{segments}".encode()).hexdigest()[:12]
    return Path(media_root) / CUTS_SUBDIR / f"{video_id}.{digest}.m4a"


def concat_list(src: Path, ranges: list[Range]) -> str:
    """An ffconcat script playing ``src`` with ``ranges`` left out: the parts
    between them, each as the same file with an in and out point. The last
    part runs to the end."""
    quoted = "'" + str(src).replace("'", "'\\''") + "'"
    kept: list[tuple[float, float | None]] = []
    start = 0.0
    for cut_start, cut_end in ranges:
        if cut_start > start:
            kept.append((start, cut_start))
        start = cut_end
    kept.append((start, None))

    lines = ["ffconcat version 1.0"]
    for inpoint, outpoint in kept:
        lines += [f"file {quoted}", f"inpoint {inpoint:.3f}"]
        if outpoint is not None:
            lines.append(f"outpoint {outpoint:.3f}")
    return "\n".join(lines) + "\n"


def cut_audio(src: Path, dst: Path, ranges: list[Range], *, timeout: int = 600) -> int | None:
    """Write ``src`` to ``dst`` without ``ranges``. Returns the result's
    duration in seconds, if ffprobe can tell."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    with tempfile.NamedTemporaryFile("w", suffix=".ffconcat") as script:
        script.write(concat_list(src, ranges))
        script.flush()
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            script.name,
            "-map",
            "0:a",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "ipod",
            str(part),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            part.unlink(missing_ok=True)
            raise CutError(f"ffmpeg timed out cutting {src}") from exc
    if result.returncode != 0:
        part.unlink(missing_ok=True)
        raise CutError(result.stderr.strip()[:2000] or f"ffmpeg failed cutting {src}")
    part.replace(dst)
    return _duration(dst)


def _duration(path: Path) -> int | None:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False).stdout
        return round(float(out.strip()))
    except (subprocess.TimeoutExpired, ValueError):
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


def _due(cut: AudioCut | None, video: Video, now: datetime) -> bool:
    """Whether to look ``cut``'s segments up again: never checked; or not
    lately, and either it failed last time or the video's recent enough that
    segments may still be arriving."""
    if cut is None:
        return True
    if now - _aware(cut.checked_at) < RECHECK_EVERY:
        return False
    return cut.segments is None or video.published_at is None or now - _aware(video.published_at) < RECHECK_FOR


def _remove(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


def refresh_audio_cuts(
    db: Session,
    media_root: str,
    *,
    now: datetime | None = None,
    lookup: Callable[[str], list[sponsorblock.Segment] | None] = sponsorblock.lookup,
    cut: Callable[[Path, Path, list[Range]], int | None] = cut_audio,
) -> int:
    """Bring the cut copies in line with what subscribers want: delete the
    ones nobody does any more, and make or re-make the rest as their
    segments come in, newest episodes first. Returns how many were cut."""
    now = now or datetime.now(UTC)
    wanted = wanted_audio_cuts(db)

    existing = {(c.video_id, c.categories): c for c in db.execute(select(AudioCut)).scalars()}
    for (video_id, kinds), row in list(existing.items()):
        if kinds not in wanted.get(video_id, ()):
            # Pruned, or the subscribers changed their minds.
            _remove(row.path)
            db.delete(row)
            del existing[(video_id, kinds)]
    db.commit()
    if not wanted:
        return 0

    episodes = db.execute(
        select(Download.audio_path, Video)
        .join(Video, Video.video_id == Download.video_id)
        .where(
            Download.video_id.in_(wanted),
            Download.status == "complete",
            Download.audio_path.is_not(None),
        )
        .order_by(Video.published_at.desc().nullslast())
    ).all()

    lookups = cuts = 0
    for audio_path, video in episodes:
        due = [k for k in sorted(wanted[video.video_id]) if _due(existing.get((video.video_id, k)), video, now)]
        if not due:
            continue
        if lookups >= _MAX_LOOKUPS or cuts >= _MAX_CUTS:
            break
        lookups += 1
        found = lookup(video.video_id)
        if found is None:
            continue  # SponsorBlock unreachable: try again next pass

        for kinds in due:
            if cuts >= _MAX_CUTS:
                break
            row = existing.get((video.video_id, kinds))
            if row is None:
                row = AudioCut(video_id=video.video_id, categories=kinds)
                db.add(row)
            row.checked_at = now
            ranges = plan(found, kinds.split(","), video.duration_seconds)
            segments = fingerprint(ranges)
            if segments == row.segments:
                db.commit()
                continue

            old = row.path
            if not ranges:
                row.segments, row.path, row.size_bytes, row.duration_seconds = "", None, None, None
            else:
                dst = cut_path(media_root, video.video_id, kinds, segments)
                cuts += 1
                try:
                    duration = cut(Path(audio_path), dst, ranges)
                except CutError as exc:
                    # Keep serving whatever there was; retried on the next check.
                    logger.warning("Couldn't cut SponsorBlock segments from %s: %s", video.video_id, exc)
                    row.segments = None
                    db.commit()
                    continue
                row.segments, row.path = segments, str(dst)
                row.size_bytes, row.duration_seconds = dst.stat().st_size, duration
            db.commit()
            if old and old != row.path:
                _remove(old)

    if cuts:
        logger.info("Cut SponsorBlock segments from %d podcast episodes", cuts)
    return cuts
