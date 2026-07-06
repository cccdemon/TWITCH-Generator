"""FFmpeg stage: cut each moment into a (optionally vertical, subtitled) clip."""
from __future__ import annotations

import os
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


def _subtitles_filter(srt_path: Path) -> str:
    return (
        f"subtitles={_escape_for_filter(srt_path)}:"
        "force_style='FontSize=18,Outline=2,Alignment=2,MarginV=60'"
    )


def _split_filtergraph(width: int, height: int, facecam: dict) -> str:
    """filter_complex: top half = facecam region, bottom half = center-cropped game."""
    h2 = height // 2
    fx = float(facecam.get("x", 0.78))
    fy = float(facecam.get("y", 0.0))
    fw = float(facecam.get("w", 0.22))
    fh = float(facecam.get("h", 0.22))
    fill = (
        f"scale={width}:{h2}:force_original_aspect_ratio=increase,"
        f"crop={width}:{h2}"
    )
    return (
        f"[0:v]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},{fill}[top];"
        f"[0:v]{fill}[bot];"
        f"[top][bot]vstack=inputs=2[stacked]"
    )


def render_clip(
    source: Path,
    moment: Moment,
    segments: list[TranscriptSegment],
    out_dir: Path,
    *,
    vertical: bool,
    width: int,
    height: int,
    layout: str = "split",
    facecam: dict | None = None,
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

    use_split = vertical and layout == "split"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}",
        "-i", str(source),
        "-t", f"{duration:.2f}",
    ]

    if use_split:
        graph = _split_filtergraph(width, height, facecam or {})
        if has_subs:
            graph += f";[stacked]{_subtitles_filter(srt_path)}[vout]"
            vlabel = "[vout]"
        else:
            vlabel = "[stacked]"
        cmd += ["-filter_complex", graph, "-map", vlabel, "-map", "0:a?"]
    else:
        filters: list[str] = []
        if vertical:  # layout == "crop": plain center-crop into 9:16
            filters.append(
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
        if has_subs:
            filters.append(_subtitles_filter(srt_path))
        if filters:
            cmd += ["-vf", ",".join(filters)]

    # Encode to a .part file, then atomically rename. The clips page globs
    # "*.mp4", so a half-written clip is never picked up mid-render.
    tmp_path = out_dir / f"{name}.mp4.part"
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(tmp_path),
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp_path, out_path)

    return Clip(moment=moment, path=str(out_path),
                srt_path=str(srt_path) if has_subs else None)
