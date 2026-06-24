"""Audio stage: per-interval loudness energy via a single ffmpeg ebur128 pass.

Loud bursts (explosions, shouting, gunfire) are a cheap, transcript-independent
highlight signal. We read EBU R128 momentary loudness (LUFS), bucket it into the
same interval grid as the vision frames, and normalize to 0..1.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# ebur128 prints lines like: "t: 12.3  M: -18.4 S: -20.1 I: -23.0 LRA: ..."
_LINE = re.compile(r"t:\s*([\d.]+)\s+M:\s*(-?[\d.]+)")
# Momentary loudness floor; quieter than this maps to 0.
_FLOOR_LUFS = -40.0


def _measure(source: Path) -> list[tuple[float, float]]:
    """Return [(t, momentary_LUFS), ...] over the whole file (one ffmpeg pass)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(source),
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    # ebur128 writes to stderr.
    out: list[tuple[float, float]] = []
    for m in _LINE.finditer(proc.stderr):
        out.append((float(m.group(1)), float(m.group(2))))
    return out


def run(source: Path, work: Path, interval: float) -> list[tuple[float, float]]:
    """Per-interval audio energy: [(bucket_start_t, score0..1), ...]. Cached."""
    cache = work / "audio.json"
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]

    samples = _measure(source)
    buckets: dict[int, float] = {}
    for t, lufs in samples:
        b = int(t // interval)
        # keep the loudest momentary value in each bucket
        buckets[b] = max(buckets.get(b, -120.0), lufs)

    result: list[tuple[float, float]] = []
    for b, lufs in sorted(buckets.items()):
        # map [_FLOOR_LUFS .. 0] LUFS -> [0 .. 1]
        score = (lufs - _FLOOR_LUFS) / (0.0 - _FLOOR_LUFS)
        score = max(0.0, min(1.0, score))
        result.append((round(b * interval, 2), round(score, 4)))

    cache.write_text(json.dumps(result), encoding="utf-8")
    return result
