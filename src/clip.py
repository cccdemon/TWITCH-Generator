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

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def slug(title: str, limit: int = 40) -> str:
    """ASCII-only file stem from a caption.

    The web routes only serve names matching [A-Za-z0-9._-]; German captions
    from the local models ("Zerstörung", "erklärt") used to produce 400s, so
    umlauts are transliterated and anything else non-ASCII is dropped.
    """
    import unicodedata
    t = unicodedata.normalize("NFKD", title.translate(_UMLAUTS)).encode("ascii", "ignore").decode()
    t = "".join(c if c.isalnum() else "_" for c in t).strip("_")
    return t[:limit].rstrip("_") or "clip"



TOP_MIN, TOP_MAX = 0.15, 0.75   # share of the output height the facecam cell may take


def clamp_top(top) -> float:
    try:
        return max(TOP_MIN, min(TOP_MAX, float(top)))
    except (TypeError, ValueError):
        return 0.5


def _venc() -> list[str]:
    """x264 settings from config.yaml `clip.preset` / `clip.crf` (defaults: veryfast / 20).

    The uploaded file is re-encoded by every platform, so a slower preset and a lower
    CRF pay off where the CPU allows it (deploy/local uses slow / 17).
    """
    preset, crf = "veryfast", 20
    try:
        from .config import load_config
        c = load_config().get("clip") or {}
        preset, crf = str(c.get("preset", preset)), int(c.get("crf", crf))
    except Exception:  # noqa: BLE001 - no config file (tests, ad-hoc use): defaults
        pass
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]


def _fill(width: int, height: int, sharpen: bool = False) -> str:
    """ffmpeg chain: scale (lanczos) to cover width x height, crop the overflow.

    `sharpen` adds a mild unsharp mask; used for the facecam, which is a small
    region of the source blown up several times.
    """
    chain = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height}"
    )
    return chain + ",unsharp=5:5:0.6:5:5:0.0" if sharpen else chain


def _box_fill(box: dict | None, width: int, height: int, sharpen: bool = False) -> str:
    """ffmpeg chain: crop a fractional region, then fill width x height."""
    b = box or {}
    bx = max(0.0, min(1.0, float(b.get("x", 0.0))))
    by = max(0.0, min(1.0, float(b.get("y", 0.0))))
    bw = max(0.02, min(1.0 - bx, float(b.get("w", 1.0))))
    bh = max(0.02, min(1.0 - by, float(b.get("h", 1.0))))
    return f"crop=iw*{bw}:ih*{bh}:iw*{bx}:ih*{by}," + _fill(width, height, sharpen)


def _stack_graph(width: int, height: int, cam: dict | None, game: dict | None,
                 top: float = 0.5) -> str:
    """filter_complex ending in [stacked]: facecam cell on top, game cell below.

    `top` is the facecam cell's share of the output height (0.5 = two halves). Both
    boxes are fractions of the source frame; `game=None` = centre crop of the frame.
    """
    h_top = int(round(height * clamp_top(top) / 2)) * 2
    return (
        f"[0:v]{_box_fill(cam, width, h_top, sharpen=True)}[top];"
        f"[0:v]{_box_fill(game, width, height - h_top)}[bot];"
        f"[top][bot]vstack=inputs=2[stacked]"
    )


_CAM_DEFAULT = {"x": 0.78, "y": 0.0, "w": 0.22, "h": 0.22}


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
    game: dict | None = None,
    top: float = 0.5,
    pad_start: float,
    pad_end: float,
    burn_subtitles: bool,
) -> Clip:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = slug(moment.title)
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
        graph = _stack_graph(width, height, {**_CAM_DEFAULT, **(facecam or {})}, game, top)
        if has_subs:
            graph += f";[stacked]{_subtitles_filter(srt_path)}[vout]"
            vlabel = "[vout]"
        else:
            vlabel = "[stacked]"
        cmd += ["-filter_complex", graph, "-map", vlabel, "-map", "0:a?"]
    else:
        filters: list[str] = []
        if vertical:  # layout == "crop": the game region (or the frame centre) fills 9:16
            filters.append(_box_fill(game, width, height))
        if has_subs:
            filters.append(_subtitles_filter(srt_path))
        if filters:
            cmd += ["-vf", ",".join(filters)]

    # Encode to a .part file, then atomically rename. The clips page globs
    # "*.mp4", so a half-written clip is never picked up mid-render.
    tmp_path = out_dir / f"{name}.mp4.part"
    cmd += [
        *_venc(),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(tmp_path),
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp_path, out_path)

    return Clip(moment=moment, path=str(out_path),
                srt_path=str(srt_path) if has_subs else None)


def render_custom(
    source: Path,
    out_path: Path,
    *,
    start: float,
    duration: float,
    width: int = 1080,
    height: int = 1920,
    layout: str = "split",
    facecam: dict | None = None,
    game: dict | None = None,
    top: float = 0.5,
    segments: list[TranscriptSegment] | None = None,
    burn_subtitles: bool = False,
    title: str = "",
) -> Path:
    """Render one clip from an explicit trim window + crop regions (GUI editor).

    layout "split": `facecam` region on top (`top` = its share of the height,
                    0.5 = halves), `game` region below.
    layout "crop":  the whole 9:16 frame is the `game` region (or center crop).
    Boxes are fractions {x,y,w,h} of the source frame; None = full frame.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(start))
    duration = max(0.5, float(duration))

    has_subs = False
    srt_path = out_path.with_suffix(".srt")
    if burn_subtitles and segments:
        m = Moment(start=start, end=start + duration, category="", score=0.0,
                   title=title or "clip", reason="")
        has_subs = write_srt(m, segments, srt_path)

    cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(source), "-t", f"{duration:.2f}"]

    if layout == "split":
        graph = _stack_graph(width, height, facecam, game, top)
        vlabel = "[stacked]"
        if has_subs:
            graph += f";[stacked]{_subtitles_filter(srt_path)}[vout]"
            vlabel = "[vout]"
        cmd += ["-filter_complex", graph, "-map", vlabel, "-map", "0:a?"]
    else:  # single-region crop into full 9:16
        chain = _box_fill(game, width, height)
        if has_subs:
            chain += f",{_subtitles_filter(srt_path)}"
        cmd += ["-vf", chain]

    tmp = out_path.with_suffix(".mp4.part")
    cmd += [
        *_venc(),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, out_path)
    return out_path


def probe_duration(path: Path) -> float:
    """Container duration in seconds (ffprobe)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def trim_clip(src: Path, out: Path, start: float, end: float) -> Path:
    """Re-encode [start, end) of an already rendered clip (no source VOD needed).

    `src` may equal `out`: the result goes to a .part file first and replaces
    `out` only when ffmpeg succeeded.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(start))
    duration = max(0.5, float(end) - start)
    tmp = out.with_suffix(".mp4.part")
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{duration:.2f}",
        "-map", "0:v", "-map", "0:a?",
        *_venc(),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, out)
    return out


def preview_segment(source: Path, out: Path, start: float, end: float) -> Path:
    """Small 480p landscape cut of [start, end) from the source VOD for the post panel's
    preview (seconds of work instead of a full-VOD proxy). Written via .part."""
    out.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(start))
    tmp = out.with_suffix(".mp4.part")
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-ss", f"{start:.2f}", "-i", str(source),
        "-t", f"{max(0.5, float(end) - start):.2f}",
        "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    os.replace(tmp, out)
    return out
