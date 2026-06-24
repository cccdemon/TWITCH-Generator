"""Shared data structures passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class VodInfo:
    vod_id: str
    url: str
    title: str = ""
    streamer: str = ""
    duration: float = 0.0


@dataclass
class VisionFrame:
    """One sampled video frame scored by Claude Vision."""
    t: float                       # timestamp (seconds)
    scores: dict[str, float]       # per-category 0..1 (explosion, pvp, ...)
    visual_score: float            # overall 0..1
    label: str                     # short description


@dataclass
class Moment:
    """One highlight window (text-detected and/or fused from audio+video)."""
    start: float
    end: float
    category: str          # funny | emotional | exciting | skillful | pvp | explosion | ...
    score: float           # 0..1 final highlight score
    title: str             # short-form caption
    reason: str            # why it was picked
    # Component scores (0..1) when produced by the fusion stage; else 0.
    audio_score: float = 0.0
    text_score: float = 0.0
    video_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Clip:
    moment: Moment
    path: str              # rendered video file
    srt_path: str | None = None


@dataclass
class UploadResult:
    platform: str
    ok: bool
    url: str | None = None
    error: str | None = None
