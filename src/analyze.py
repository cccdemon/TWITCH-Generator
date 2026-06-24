"""LLM stage: Claude scans the transcript and marks highlight windows.

Strategy: split the transcript into overlapping time chunks, ask Claude for the
strongest moments per chunk (structured JSON via a tool), then merge + rank.
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from .config import env
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
) -> list[Moment]:
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))

    system = (
        "You find short-form-worthy moments in Twitch stream transcripts. "
        f"Categories: {', '.join(categories)}. "
        f"Each moment must be {min_clip_seconds:.0f}-{max_clip_seconds:.0f}s long, "
        "self-contained, and land a clear payoff (laugh, big play, raw emotion, tension spike). "
        "Use only timestamps present in the transcript. Be selective; quality over quantity."
    )

    found: list[Moment] = []
    for chunk in _chunks(segments):
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=[_MOMENT_TOOL],
            tool_choice={"type": "tool", "name": "report_moments"},
            messages=[{
                "role": "user",
                "content": (
                    "Transcript chunk (timestamps in seconds):\n\n"
                    + _render(chunk)
                    + "\n\nReport the strongest moments via report_moments."
                ),
            }],
        )
        found.extend(_parse(msg, categories, min_clip_seconds, max_clip_seconds))

    found.sort(key=lambda m: m.score, reverse=True)
    return _dedupe(found)[:max_clips]


def _parse(msg: Any, categories: list[str], lo: float, hi: float) -> list[Moment]:
    out: list[Moment] = []
    for block in msg.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        for m in block.input.get("moments", []):
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
            out.append(Moment(
                start=round(start, 2),
                end=round(end, 2),
                category=cat if cat in categories else categories[0],
                score=max(0.0, min(1.0, float(m.get("score", 0.5)))),
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
