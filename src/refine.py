"""Refine stage: one global Claude pass over all fused candidates.

The per-chunk text pass and the per-batch vision pass each score in isolation,
so their numbers are not comparable. Here Claude sees EVERY candidate side by
side, with its transcript context and signal hints, and:
  - keeps only the ones with a real hook + payoff (may reject all),
  - re-scores them on one shared rubric,
  - moves start/end onto natural boundaries (sentence starts, after the punchline),
  - writes the caption.

Boundaries are then snapped to transcript segment edges deterministically so a
clip never opens or closes mid-word. If the API call fails, the fused ranking is
kept and only the deterministic snapping is applied.
"""
from __future__ import annotations

from typing import Any

from .config import env
from .models import Moment, TranscriptSegment

_TOOL = {
    "name": "finalize_clips",
    "description": "Return the final short-form clip selection from the candidate list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "candidate index"},
                        "start": {"type": "number", "description": "clip start (seconds)"},
                        "end": {"type": "number", "description": "clip end (seconds)"},
                        "score": {"type": "number", "description": "0..1 on the shared rubric"},
                        "category": {"type": "string"},
                        "title": {"type": "string", "description": "punchy caption, <8 words"},
                        "reason": {"type": "string", "description": "hook + payoff, one line"},
                    },
                    "required": ["index", "start", "end", "score", "category", "title", "reason"],
                },
            }
        },
        "required": ["clips"],
    },
}

_RUBRIC = (
    "Scoring rubric (apply the SAME scale to every candidate):\n"
    "  0.9-1.0  would stop a scroll cold: huge laugh, insane play, raw emotion, chat exploded\n"
    "  0.7-0.8  clearly clip-worthy, obvious payoff within the window\n"
    "  0.5-0.6  decent but needs context or the payoff is mild\n"
    "  <0.5     filler, setup without payoff, or the signals are noise — DROP IT\n"
)


def context_text(segments: list[TranscriptSegment], start: float, end: float, pad: float) -> str:
    lo, hi = start - pad, end + pad
    lines = [f"[{s.start:.0f}-{s.end:.0f}] {s.text}" for s in segments if s.end >= lo and s.start <= hi]
    return "\n".join(lines) if lines else "(no speech)"


def snap_to_segments(
    start: float, end: float, segments: list[TranscriptSegment], *,
    tolerance: float, min_len: float, max_len: float,
) -> tuple[float, float]:
    """Move start onto a segment start and end onto a segment end within
    `tolerance` seconds, then enforce the length window.

    Bias: a start prefers an EARLIER segment start (never cut the hook), an end
    prefers a LATER segment end (never cut the punchline). Only if nothing lies
    on the preferred side is the nearest edge on the other side used."""
    def pick(target: float, edges: list[float], prefer_before: bool) -> float:
        near = [e for e in edges if abs(e - target) < tolerance]
        if not near:
            return target
        preferred = [e for e in near if (e <= target if prefer_before else e >= target)]
        pool = preferred or near
        return min(pool, key=lambda e: abs(e - target))

    best_s = pick(start, [s.start for s in segments], prefer_before=True)
    best_e = pick(end, [s.end for s in segments if s.end > best_s], prefer_before=False)
    s0, e0 = max(0.0, best_s), best_e
    if e0 - s0 < min_len:
        e0 = s0 + min_len
    if e0 - s0 > max_len:
        # Too long: pull the end back to the last sentence end that still fits,
        # rather than cutting mid-sentence at the hard limit.
        limit = s0 + max_len
        fitting = [s.end for s in segments if s0 + min_len <= s.end <= limit]
        e0 = max(fitting) if fitting else limit
    return round(s0, 2), round(e0, 2)


def _overlap(a: Moment, b: Moment) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shorter = min(a.end - a.start, b.end - b.start) or 1.0
    return inter / shorter


def _dedupe(moments: list[Moment]) -> list[Moment]:
    kept: list[Moment] = []
    for m in moments:
        if any(_overlap(m, k) > 0.5 for k in kept):
            continue
        kept.append(m)
    return kept


def _apply(
    chosen: list[dict[str, Any]], candidates: list[Moment],
    segments: list[TranscriptSegment], categories: list[str], *,
    min_len: float, max_len: float, tolerance: float,
) -> list[Moment]:
    out: list[Moment] = []
    for c in chosen:
        try:
            idx = int(c["index"])
            if not 0 <= idx < len(candidates):
                continue
            base = candidates[idx]
            start, end = float(c["start"]), float(c["end"])
            score = float(c.get("score", base.score))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        # Don't let the LLM wander far from the evidence window.
        start = max(base.start - 20.0, start)
        end = min(base.end + 20.0, end)
        start, end = snap_to_segments(start, end, segments, tolerance=tolerance,
                                      min_len=min_len, max_len=max_len)
        cat = str(c.get("category", base.category))
        out.append(Moment(
            start=start, end=end,
            category=cat if cat in categories else base.category,
            score=round(max(0.0, min(1.0, score)), 4),
            title=str(c.get("title") or base.title)[:80],
            reason=str(c.get("reason") or base.reason),
            audio_score=base.audio_score, text_score=base.text_score,
            video_score=base.video_score, chat_score=base.chat_score,
            hints=base.hints,
        ))
    out.sort(key=lambda m: m.score, reverse=True)
    return _dedupe(out)


def _fallback(candidates: list[Moment], segments: list[TranscriptSegment], *,
              max_clips: int, min_len: float, max_len: float, tolerance: float) -> list[Moment]:
    out: list[Moment] = []
    for m in candidates[:max_clips]:
        s, e = snap_to_segments(m.start, m.end, segments, tolerance=tolerance,
                                min_len=min_len, max_len=max_len)
        out.append(Moment(**{**m.to_dict(), "start": s, "end": e}))
    return out


def refine(
    candidates: list[Moment],
    segments: list[TranscriptSegment],
    cfg: dict,
    *,
    meter=None,
    log=print,
) -> list[Moment]:
    a = cfg.get("analyze", {})
    r = cfg.get("refine", {})
    max_clips = int(a.get("max_clips", 10))
    min_len = float(a.get("min_clip_seconds", 15))
    max_len = float(a.get("max_clip_seconds", 60))
    tolerance = float(r.get("snap_tolerance_seconds", 2.5))
    pad = float(r.get("context_pad_seconds", 20))
    categories = list(a.get("categories", [])) + list(cfg.get("vision", {}).get("categories", []))
    model = r.get("model") or a.get("model", "claude-sonnet-4-6")

    if not candidates:
        return []
    if not r.get("enabled", True):
        return _fallback(candidates, segments, max_clips=max_clips, min_len=min_len,
                         max_len=max_len, tolerance=tolerance)

    blocks: list[str] = []
    for i, m in enumerate(candidates):
        blocks.append(
            f"### candidate {i}\n"
            f"window: {m.start:.0f}-{m.end:.0f}s  fused={m.score:.2f}  "
            f"(chat={m.chat_score:.2f} audio={m.audio_score:.2f} "
            f"text={m.text_score:.2f} video={m.video_score:.2f})\n"
            f"category guess: {m.category}; first-pass note: {m.reason}\n"
            f"hints: {m.hints or '-'}\n"
            f"transcript (with context):\n{context_text(segments, m.start, m.end, pad)}\n"
        )

    system = (
        "You are the final editor for short-form clips (TikTok/Shorts/Reels) cut from a "
        "Twitch stream. You get candidate windows found by chat spikes, loudness bursts, a "
        "transcript scan and a video scan, each with transcript context. Pick the best "
        f"at most {max_clips}. Rules:\n"
        f"- Each clip is {min_len:.0f}-{max_len:.0f}s. The hook must land in the first 3 seconds; "
        "end right after the payoff (laugh, kill, crash, reaction), never mid-sentence.\n"
        "- Prefer windows where several signals agree (chat + audio + video).\n"
        "- Chat reacts a few seconds AFTER the moment; the moment is just before the chat peak.\n"
        "- Use only timestamps that appear in the transcript lines or the window.\n"
        "- Categories: " + ", ".join(categories) + ".\n"
        "- Captions in the language of the stream, punchy, no hashtags.\n"
        + _RUBRIC
        + "It is fine to return fewer clips, or none, if nothing clears 0.5."
    )

    from anthropic import Anthropic  # lazy: keeps this module importable without the SDK
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "finalize_clips"},
            messages=[{"role": "user", "content": "\n".join(blocks)
                       + "\n\nReturn the final selection via finalize_clips."}],
        )
    except Exception as e:  # noqa: BLE001 — degrade to the fused ranking
        log(f"[yellow]refine pass failed ({type(e).__name__}: {e}) — keeping fused ranking")
        return _fallback(candidates, segments, max_clips=max_clips, min_len=min_len,
                         max_len=max_len, tolerance=tolerance)
    if meter is not None:
        meter.add(model, msg.usage)

    chosen: list[dict[str, Any]] = []
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            chosen.extend(block.input.get("clips", []))
    final = _apply(chosen, candidates, segments, categories,
                   min_len=min_len, max_len=max_len, tolerance=tolerance)
    return final[:max_clips]
