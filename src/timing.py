"""Processing-time prediction.

Total pipeline time scales roughly with VOD length (Whisper transcription
dominates). We keep a learned "speed factor" = processing_seconds / vod_seconds
in data/timing.json, seeded with a default and refined (EMA) after each job.

Per-run state lives in data/<vod>/run.json so the web UI can show a live
"~N min left" for the running job.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import data_dir

# Fallback until we have real samples. Whisper "small" int8 on CPU (1 thread) +
# vision + ffmpeg render lands well under real-time; 0.6x is a safe first guess.
_DEFAULT_FACTOR = 0.6
_EMA_ALPHA = 0.3          # weight of the newest sample
_FACTOR_MIN, _FACTOR_MAX = 0.05, 5.0   # clamp observed samples to sane range


def _timing_path() -> Path:
    return data_dir() / "timing.json"


def _load() -> dict:
    p = _timing_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"factor": _DEFAULT_FACTOR, "samples": 0}


def factor() -> float:
    return float(_load().get("factor", _DEFAULT_FACTOR))


def estimate_seconds(vod_seconds: float) -> float:
    """Predicted total processing time for a VOD of `vod_seconds`."""
    if vod_seconds <= 0:
        return 0.0
    return vod_seconds * factor()


def record(vod_seconds: float, elapsed_seconds: float) -> None:
    """Fold a completed job's actual time into the learned factor (EMA)."""
    if vod_seconds <= 0 or elapsed_seconds <= 0:
        return
    observed = max(_FACTOR_MIN, min(_FACTOR_MAX, elapsed_seconds / vod_seconds))
    state = _load()
    if state.get("samples", 0) <= 0:
        new = observed  # first real sample replaces the seed outright
    else:
        new = state["factor"] * (1 - _EMA_ALPHA) + observed * _EMA_ALPHA
    state["factor"] = round(new, 4)
    state["samples"] = int(state.get("samples", 0)) + 1
    try:
        _timing_path().write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def probe_duration(media: Path) -> float:
    """Media duration in seconds via ffprobe, or 0.0 if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(media)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, OSError):
        return 0.0


# ---------------------------------------------------------------- per-run state

def write_run(work: Path, *, started: float, vod_seconds: float, eta_seconds: float) -> None:
    try:
        (work / "run.json").write_text(
            json.dumps({
                "started": started,
                "vod_seconds": round(vod_seconds, 1),
                "eta_seconds": round(eta_seconds, 1),
            }),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_run(vod_id: str) -> dict | None:
    p = data_dir() / vod_id / "run.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def remaining_seconds(vod_id: str) -> float | None:
    """Seconds left for a running job, from its run.json. None if unknown."""
    run = read_run(vod_id)
    if not run or not run.get("eta_seconds"):
        return None
    left = float(run["eta_seconds"]) - (time.time() - float(run.get("started", 0)))
    return max(0.0, left)
