"""download_vod: fallback to the ffmpeg downloader.

Twitch serves some VODs as fMP4 HLS with more than one init segment
(`#EXT-X-MAP` again after a `#EXT-X-DISCONTINUITY`, e.g. after a stream
reconnect). yt-dlp's native HLS downloader refuses those with
"Initialization fragment found after media fragments"; ffmpeg reads them.
"""
import subprocess

import pytest

from src import twitch
from src.models import VodInfo

INFO = VodInfo(vod_id="123", url="https://www.twitch.tv/videos/123")


def _fake_run(calls, fail_native=True, fail_ffmpeg=False):
    def run(cmd, check=True, **kw):
        calls.append(cmd)
        ffmpeg = "--downloader" in cmd
        if (ffmpeg and fail_ffmpeg) or (not ffmpeg and fail_native):
            raise subprocess.CalledProcessError(1, cmd)
        out = cmd[cmd.index("-o") + 1]
        with open(out, "wb") as f:
            f.write(b"x")
        return subprocess.CompletedProcess(cmd, 0)
    return run


def test_native_download_is_tried_first_and_kept(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(twitch.subprocess, "run", _fake_run(calls, fail_native=False))
    out = twitch.download_vod(INFO, tmp_path)
    assert out == tmp_path / "123.mp4"
    assert len(calls) == 1 and "--downloader" not in calls[0]


def test_falls_back_to_ffmpeg_downloader(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(twitch.subprocess, "run", _fake_run(calls))
    (tmp_path / "123.mp4.part").write_bytes(b"")      # leftover of the failed try
    (tmp_path / "123.mp4.ytdl").write_bytes(b"{}")
    out = twitch.download_vod(INFO, tmp_path)
    assert out == tmp_path / "123.mp4" and out.stat().st_size > 0
    assert len(calls) == 2
    i = calls[1].index("--downloader")
    assert calls[1][i + 1] == "ffmpeg"
    assert not (tmp_path / "123.mp4.ytdl").exists()


def test_raises_when_both_downloaders_fail(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(twitch.subprocess, "run", _fake_run(calls, fail_ffmpeg=True))
    with pytest.raises(subprocess.CalledProcessError):
        twitch.download_vod(INFO, tmp_path)
    assert len(calls) == 2
