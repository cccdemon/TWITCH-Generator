"""LLM stage: the text model scans the transcript and marks highlight windows.

Strategy: split the transcript into overlapping time chunks, ask the model for
candidate moments per chunk (structured JSON, empty list allowed), then merge.
This is a CANDIDATE pass: scores are only comparable within a chunk. The refine
stage re-scores all candidates side by side with full context.

All model calls go through src/llm.py (provider anthropic | ollama).
"""
from __future__ import annotations

import json
from typing import Any

from . import llm
from .llm import TextPart
from .models import Moment, TranscriptSegment

# Transcript window (seconds) sent per LLM call. Keeps each prompt bounded.
_CHUNK_SECONDS = 600
_CHUNK_OVERLAP = 30

_MOMENT_TOOL = {
    "name": "report_moments",
    "description": "Report the strongest short-form-worthy moments found in the transcript chunk.",
    "input_schema": {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "start time in seconds"},
                        "end": {"type": "number", "description": "end time in seconds"},
                        "category": {"type": "string"},
                        "score": {"type": "number", "description": "0..1 strength"},
                        "title": {"type": "string", "description": "punchy <8 word caption"},
                        "reason": {"type": "string"},
                    },
                    "required": ["start", "end", "category", "score", "title", "reason"],
                },
            }
        },
        "required": ["moments"],
    },
}


def _chunks(segments: list[TranscriptSegment]) -> list[list[TranscriptSegment]]:
    if not segments:
        return []
    out: list[list[TranscriptSegment]] = []
    window_start = segments[0].start
    cur: list[TranscriptSegment] = []
    for seg in segments:
        if seg.start - window_start > _CHUNK_SECONDS and cur:
            out.append(cur)
            # restart with overlap tail
            window_start = seg.start - _CHUNK_OVERLAP
            cur = [s for s in cur if s.end >= window_start]
        cur.append(seg)
    if cur:
        out.append(cur)
    return out


def _render(segments: list[TranscriptSegment]) -> str:
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segments)


def analyze(
    segments: list[TranscriptSegment],
    model: str,
    categories: list[str],
    max_clips: int,
    min_clip_seconds: float,
    max_clip_seconds: float,
    meter=None,
    cfg: dict | None = None,
    log=print,
) -> list[Moment]:
    system = (
        "You scan a Twitch stream transcript chunk for moments that could work as a "
        "short-form clip (TikTok/Shorts). "
        f"Categories: {', '.join(categories)}. "
        f"Each moment is {min_clip_seconds:.0f}-{max_clip_seconds:.0f}s, starts at a sentence "
        "start and ends right after a clear payoff (laugh, big play, raw emotion, tension spike). "
        "Use only timestamps present in the transcript. The transcript is machine-generated "
        "and noisy; ignore garbled lines. "
        "Scoring: 0.9+ = would stop a scroll cold; 0.7 = clearly clip-worthy; 0.5 = mild payoff; "
        "below 0.5 do not report. Most chunks of a stream contain NOTHING worth clipping — "
        "return an empty list then. Report at most 5 per chunk."
    )

    found: list[Moment] = []
    for chunk in _chunks(segments):
        data = llm.structured(
            role="text",
            model=model,
            max_tokens=2048,
            system=system,
            content=[TextPart(
                "Transcript chunk (timestamps in seconds):\n\n"
                + _render(chunk)
                + "\n\nReport the strongest moments via report_moments."
            )],
            schema=_MOMENT_TOOL["input_schema"],
            tool_name=_MOMENT_TOOL["name"],
            tool_description=_MOMENT_TOOL["description"],
            cfg=cfg,
            meter=meter,
            log=log,
        )
        found.extend(_parse(data, categories, min_clip_seconds, max_clip_seconds))

    found.sort(key=lambda m: m.score, reverse=True)
    return _dedupe(found)[:max_clips]  # callers pass a candidate budget, not the final count


def _parse(data: dict[str, Any], categories: list[str], lo: float, hi: float) -> list[Moment]:
    out: list[Moment] = []
    for m in (data.get("moments") or []):
        if not isinstance(m, dict):
            continue
        try:
            start = float(m["start"])
            end = float(m["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        # clamp duration to config window
        if end - start < lo:
            end = start + lo
        if end - start > hi:
            end = start + hi
        cat = m.get("category", categories[0])
        try:
            score = float(m.get("score", 0.5))
        except (TypeError, ValueError):
            score = 0.5
        out.append(Moment(
            start=round(start, 2),
            end=round(end, 2),
            category=cat if cat in categories else categories[0],
            score=max(0.0, min(1.0, score)),
            title=str(m.get("title", "Clip"))[:80],
            reason=str(m.get("reason", "")),
        ))
    return out


def _dedupe(moments: list[Moment]) -> list[Moment]:
    """Drop moments whose windows heavily overlap an already-kept higher-scored one."""
    kept: list[Moment] = []
    for m in moments:
        if any(_overlap(m, k) > 0.5 for k in kept):
            continue
        kept.append(m)
    return kept


def _overlap(a: Moment, b: Moment) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shorter = min(a.end - a.start, b.end - b.start) or 1.0
    return inter / shorter


def save_moments(moments: list[Moment], path) -> None:
    path.write_text(
        json.dumps([m.to_dict() for m in moments], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
