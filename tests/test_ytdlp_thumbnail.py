"""The downloader must name the episode thumbnail ``<video>-thumb.jpg``.

Jellyfin ignores a same-named ``<video>.jpg`` for episodes, so the wrong name
means no episode art. We assert the yt-dlp invocation carries the per-type
``thumbnail:`` output template rather than actually shelling out to YouTube.
"""
from __future__ import annotations

from pathlib import Path

from youtube_subs_opml.downloader import ytdlp


def test_download_uses_thumb_suffix_output_template(tmp_path, monkeypatch):
    target = tmp_path / "Chan - S2026E07 - Title"
    captured = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Emulate yt-dlp producing the merged file so download() returns cleanly.
        target.with_suffix(".mkv").write_bytes(b"mkv")
        return _Result()

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)

    produced = ytdlp.download(
        "vid00000001", target, video_format="best", write_thumbnail=True
    )
    assert produced == target.with_suffix(".mkv")

    cmd = captured["cmd"]
    assert "--write-thumbnail" in cmd
    # The thumbnail gets its own output template ending in -thumb, so the file
    # lands as "<basename>-thumb.jpg" (after --convert-thumbnails jpg).
    thumb_templates = [
        c for c in cmd if isinstance(c, str) and c.startswith("thumbnail:")
    ]
    assert thumb_templates == [f"thumbnail:{target}-thumb.%(ext)s"]


def test_download_can_skip_thumbnail(tmp_path, monkeypatch):
    target = tmp_path / "Chan - S2026E07 - Title"

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        target.with_suffix(".mkv").write_bytes(b"mkv")
        fake_run.cmd = cmd
        return _Result()

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    ytdlp.download("vid00000001", target, video_format="best", write_thumbnail=False)
    assert "--write-thumbnail" not in fake_run.cmd
