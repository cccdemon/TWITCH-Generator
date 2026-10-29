"""Single-pass media extraction.

Whisper needs 16 kHz mono WAV chunks, vision needs a frame grid, audio needs
ebur128 loudness. Each used to decode the multi-GB VOD on its own; here one
ffmpeg invocation with several outputs decodes once and produces whatever is
still missing. Every consumer stage keeps its own cache check, so this is purely
an accelerator: skipping it changes nothing but speed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import audio as audio_stage
from . import transcribe as transcribe_stage
from . import vision as vision_stage


def prepare(media: Path, work: Path, cfg: dict, *, log=print) -> None:
    w = cfg.get("whisper", {})
    v = cfg.get("vision", {})
    c = cfg.get("clip", {})
    chunk_seconds = int(w.get("chunk_seconds", 1800))
    chunk_dir = work / "audio_chunks"
    want_wav = not (work / "transcript.json").exists() \
        and not transcribe_stage.chunks_ready(chunk_dir, chunk_seconds)
    frames_dir = work / "frames"
    interval = float(v.get("interval_seconds", 10))
    width = int(v.get("frame_width", 640))
    # Frames feed vision AND the facecam auto-detect, which runs even with vision off.
    needs_frames = bool(v.get("enabled", True)) or (
        c.get("vertical", True) and c.get("layout", "split") == "split" and c.get("facecam_auto", True))
    want_frames = needs_frames and not (work / "vision.json").exists() \
        and not vision_stage.frames_ready(frames_dir, interval, width)
    want_loud = bool(cfg.get("audio", {}).get("enabled", True)) \
        and not (work / "audio.json").exists() and not (work / "loudness.json").exists()

    if not (want_wav or want_frames or want_loud):
        return

    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-nostats", "-i", str(media)]
    parts = []
    if want_wav:
        transcribe_stage.reset_chunks(chunk_dir)
        cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                "-f", "segment", "-segment_time", str(chunk_seconds),
                str(chunk_dir / "chunk_%04d.wav")]
        parts.append("wav chunks")
    if want_frames:
        vision_stage.reset_frames(frames_dir)
        cmd += ["-map", "0:v:0", "-an", "-vf", vision_stage.frame_filter(interval, width),
                "-q:v", "5", str(frames_dir / "f_%05d.jpg")]
        parts.append("frames")
    if want_loud:
        cmd += ["-map", "0:a:0", "-vn", "-af", "ebur128", "-f", "null", "-"]
        parts.append("loudness")

    log(f"  single-pass decode: {', '.join(parts)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # Fall back to per-stage extraction; surface the tail of ffmpeg's complaint.
        log(f"[yellow]single-pass decode failed (rc={proc.returncode}); stages will extract on their own")
        log(proc.stderr[-800:])
        # Never leave partial outputs behind: a truncated chunk set would be
        # picked up as complete and silently shorten the transcript.
        if want_wav:
            transcribe_stage.reset_chunks(chunk_dir)
        if want_frames:
            vision_stage.reset_frames(frames_dir)
        return

    if want_wav:
        transcribe_stage.write_chunks_meta(chunk_dir, chunk_seconds)
    if want_frames:
        vision_stage.write_frames_meta(frames_dir, interval, width)
    if want_loud:
        samples = audio_stage.parse_ebur128(proc.stderr)
        if samples:  # empty = parse miss; let audio.run measure again rather than cache zeros
            try:
                (work / "loudness.json").write_text(json.dumps(samples), encoding="utf-8")
            except OSError:
                pass
