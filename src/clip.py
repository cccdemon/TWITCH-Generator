"""FFmpeg stage: cut each moment into a (optionally vertical, subtitled) clip."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .models import Clip, Moment, TranscriptSegment


def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(moment: Moment, segments: list[TranscriptSegment], srt_path: Path) -> bool:
    """Write subtitles for the clip window, timestamps rebased to clip start."""
    lines: list[str] = []
    idx = 1
    for seg in segments:
        if seg.end <= moment.start or seg.start >= moment.end:
            continue
        start = max(seg.start, moment.start) - moment.start
        end = min(seg.end, moment.end) - moment.start
        if end <= start:
            continue
        lines.append(str(idx))
        lines.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        lines.append(seg.text)
        lines.append("")
        idx += 1
    if not lines:
        return False
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _escape_for_filter(path: Path) -> str:
    # ffmpeg subtitles filter needs ':' and '\' escaped; forward slashes work cross-platform.
    p = str(path).replace("\\", "/")
    return p.replace(":", "\\:")


def render_clip(
    source: Path,
    moment: Moment,
    segments: list[TranscriptSegment],
    out_dir: Path,
    *,
    vertical: bool,
    width: int,
    height: int,
    pad_start: float,
    pad_end: float,
    burn_subtitles: bool,
) -> Clip:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(c if c.isalnum() else "_" for c in moment.title)[:40] or "clip"
    name = f"{int(moment.start)}_{moment.category}_{safe_title}"
    out_path = out_dir / f"{name}.mp4"

    start = max(0.0, moment.start - pad_start)
    duration = (moment.end + pad_end) - start

    filters: list[str] = []
    if vertical:
        filters.append(
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )

    srt_path = out_dir / f"{name}.srt"
    has_subs = False
    if burn_subtitles:
        # SRT rebased to clip start, but we also pad start; offset subs by pad.
        offset_moment = Moment(
            start=start, end=moment.end + pad_end,
            category=moment.category, score=moment.score,
            title=moment.title, reason=moment.reason,
        )
        has_subs = write_srt(offset_moment, segments, srt_path)
        if has_subs:
            filters.append(
                f"subtitles={_escape_for_filter(srt_path)}:"
                "force_style='FontSize=18,Outline=2,Alignment=2,MarginV=60'"
            )

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}",
        "-i", str(source),
        "-t", f"{duration:.2f}",
    ]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)

    return Clip(moment=moment, path=str(out_path),
                srt_path=str(srt_path) if has_subs else None)
