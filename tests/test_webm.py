"""Rewriting new downloads from Matroska to WebM: reading the header and
deciding what fits.

ffmpeg/ffprobe are faked; the conversion itself is a plain stream copy.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from youtube_subs_opml.downloader import webm

EBML = b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\xf7\x81\x01"


def header(doctype: bytes) -> bytes:
    return EBML + b"\x42\x82" + bytes([0x80 | len(doctype)]) + doctype + b"\x00" * 40


def test_doctype(tmp_path):
    for name in ("matroska", "webm"):
        (tmp_path / name).write_bytes(header(name.encode()))
        assert webm.doctype(tmp_path / name) == name
    (tmp_path / "junk").write_bytes(b"not a video")
    assert webm.doctype(tmp_path / "junk") is None
    assert webm.doctype(tmp_path / "missing") is None


def _probe(monkeypatch, streams):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"streams": streams}), stderr="")

    monkeypatch.setattr(webm.subprocess, "run", run)


@pytest.mark.parametrize(
    ("streams", "fits"),
    [
        ([{"codec_type": "video", "codec_name": "av1"}, {"codec_type": "audio", "codec_name": "opus"}], True),
        ([{"codec_type": "video", "codec_name": "vp9"}, {"codec_type": "audio", "codec_name": "opus"}], True),
        ([{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "opus"}], False),
        ([{"codec_type": "video", "codec_name": "vp9"}, {"codec_type": "audio", "codec_name": "aac"}], False),
        ([{"codec_type": "video", "codec_name": "vp9"}, {"codec_type": "attachment", "codec_name": "ttf"}], False),
        ([], False),
    ],
)
def test_fits_webm(monkeypatch, tmp_path, streams, fits):
    _probe(monkeypatch, streams)
    assert webm.fits_webm(tmp_path / "x.mkv") is fits


def test_unfit_matroska_is_left_alone(monkeypatch, tmp_path):
    path = tmp_path / "ep.mkv"
    path.write_bytes(header(b"matroska"))
    probes = []

    def run(cmd, **kw):
        probes.append(cmd[0])
        streams = [{"codec_type": "video", "codec_name": "h264"}]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"streams": streams}), stderr="")

    monkeypatch.setattr(webm.subprocess, "run", run)
    assert webm.convert(path) is False
    assert probes == ["ffprobe"]  # probed, never remuxed


def test_already_webm_is_left_alone(monkeypatch, tmp_path):
    path = tmp_path / "ep.mkv"
    path.write_bytes(header(b"webm"))
    monkeypatch.setattr(webm.subprocess, "run", lambda *a, **k: pytest.fail("shouldn't run anything"))
    assert webm.convert(path) is False
