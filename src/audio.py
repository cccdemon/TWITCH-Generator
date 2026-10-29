"""Audio stage: loudness bursts RELATIVE to the stream's own level.

Twitch streams are loudness-normalised and the mic sits at a steady level, so an
absolute LUFS scale makes every bucket look "loud". We take the momentary EBU
R128 loudness (ffmpeg ebur128), keep the peak per bucket, and score each bucket
by how far it rises above the rolling median of its surroundings. +scale dB
above the local norm == 1.0.

Raw samples are cached in loudness.json (also written by the single-pass
extractor in extract.py); the scored signal in audio.json.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .signals import Signal, bucketize, relative

_CACHE_VERSION = 2

# ebur128 prints lines like:
#   "[Parsed_ebur128_0 @ ...] t: 12.3   TARGET:-23 LUFS    M: -18.4 S: -20.1  I: -23.0 LUFS  LRA: ..."
# The TARGET field is optional (older ffmpeg omits it); M can be "-inf" for silence.
_LINE = re.compile(r"t:\s*([\d.]+)\s+(?:TARGET:\S+\s+LUFS\s+)?M:\s*(-?[\d.]+|-inf)")
_SILENCE = -70.0   # ebur128 reports -inf-ish values for silence; clamp


def parse_ebur128(stderr: str) -> list[tuple[float, float]]:
    """[(t, momentary_LUFS)] from ffmpeg's ebur128 stderr output."""
    out: list[tuple[float, float]] = []
    for m in _LINE.finditer(stderr):
        raw = m.group(2)
        lufs = _SILENCE if raw == "-inf" else max(_SILENCE, float(raw))
        out.append((float(m.group(1)), lufs))
    return out


def _measure(source: Path) -> list[tuple[float, float]]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(source),
         "-vn", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ebur128 failed (rc={proc.returncode}): {proc.stderr[-300:]}")
    return parse_ebur128(proc.stderr)


def load_loudness(work: Path, source: Path) -> list[tuple[float, float]]:
    """Raw momentary loudness samples, measured once and cached."""
    cache = work / "loudness.json"
    if cache.exists():
        try:
            return [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        except (json.JSONDecodeError, OSError):
            pass
    samples = _measure(source)
    if samples:  # never cache an empty measurement; leave it retryable
        try:
            cache.write_text(json.dumps(samples), encoding="utf-8")
        except OSError:
            pass
    return samples


def score_loudness(
    samples: list[tuple[float, float]], *, interval: float,
    baseline_seconds: float, scale_db: float,
) -> Signal:
    grid = bucketize(samples, interval, reduce=max, empty=_SILENCE)
    return relative(grid, interval=interval, baseline_seconds=baseline_seconds,
                    scale=scale_db, mode="diff")


def run(source: Path, work: Path, cfg: dict) -> Signal:
    """Per-bucket audio burst score [(bucket_t, 0..1)]. Cached."""
    a = cfg.get("audio", {})
    s = cfg.get("signals", {})
    params = {
        "v": _CACHE_VERSION,
        "interval": float(s.get("interval_seconds", 10)),
        "baseline": float(s.get("baseline_window_seconds", 300)),
        "scale_db": float(a.get("scale_db", 8.0)),
    }
    cache = work / "audio.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("params") == params:
                return [tuple(x) for x in data["data"]]
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    samples = load_loudness(work, source)
    if not samples:
        raise RuntimeError("ebur128 produced no loudness samples")
    sig = score_loudness(samples, interval=params["interval"],
                         baseline_seconds=params["baseline"], scale_db=params["scale_db"])
    try:
        cache.write_text(json.dumps({"params": params, "data": sig}), encoding="utf-8")
    except OSError:
        pass
    return sig
