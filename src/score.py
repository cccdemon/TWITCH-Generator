"""Fusion stage: combine audio + text + video signals into ranked highlights.

    highlight = wA*audio_score + wT*text_score + wV*video_score

Candidate windows come from BOTH the transcript moments (text) and clusters of
high-scoring vision frames (so silent visual moments still surface). Each candidate
is then scored against all three signals over its window.
"""
from __future__ import annotations

import json

from .models import Moment, VisionFrame


def _norm_weights(w: dict) -> tuple[float, float, float]:
    a = float(w.get("audio", 0.25))
    t = float(w.get("text", 0.35))
    v = float(w.get("video", 0.40))
    s = a + t + v or 1.0
    return a / s, t / s, v / s


def _vision_clusters(
    frames: list[VisionFrame], interval: float, threshold: float,
    min_len: float, max_len: float,
) -> list[tuple[float, float, VisionFrame]]:
    """Group consecutive high-`visual_score` frames into windows.

    Returns (start, end, peak_frame) — peak_frame supplies the label/category.
    """
    windows: list[tuple[float, float, VisionFrame]] = []
    cur: list[VisionFrame] = []

    def flush() -> None:
        if not cur:
            return
        start = cur[0].t
        end = cur[-1].t + interval
        if end - start < min_len:
            end = start + min_len
        if end - start > max_len:
            end = start + max_len
        peak = max(cur, key=lambda f: f.visual_score)
        windows.append((start, end, peak))

    prev_t = None
    for f in frames:
        if f.visual_score < threshold:
            flush(); cur = []; prev_t = None
            continue
        if prev_t is not None and f.t - prev_t > interval * 1.6:
            flush(); cur = []
        cur.append(f)
        prev_t = f.t
    flush()
    return windows


def _max_audio(audio: list[tuple[float, float]], start: float, end: float) -> float:
    return max((s for t, s in audio if start <= t <= end), default=0.0)


def _vision_in(frames: list[VisionFrame], start: float, end: float) -> list[VisionFrame]:
    return [f for f in frames if start <= f.t <= end]


def _top_category(frames: list[VisionFrame], fallback: str) -> tuple[str, str]:
    """Dominant vision category + label across the given frames."""
    if not frames:
        return fallback, ""
    peak = max(frames, key=lambda f: f.visual_score)
    cat = max(peak.scores, key=peak.scores.get) if peak.scores else fallback
    return cat, peak.label


def fuse(
    text_moments: list[Moment],
    vision_frames: list[VisionFrame],
    audio_energy: list[tuple[float, float]],
    cfg: dict,
) -> list[Moment]:
    score_cfg = cfg.get("score", {})
    wA, wT, wV = _norm_weights(score_cfg.get("weights", {}))
    a_cfg = cfg.get("analyze", {})
    v_cfg = cfg.get("vision", {})
    min_len = a_cfg.get("min_clip_seconds", 15)
    max_len = a_cfg.get("max_clip_seconds", 60)
    interval = v_cfg.get("interval_seconds", 30)
    vthresh = v_cfg.get("highlight_threshold", 0.5)
    max_clips = a_cfg.get("max_clips", 10)

    candidates: list[tuple[float, float, Moment | None]] = []
    for m in text_moments:
        candidates.append((m.start, m.end, m))
    for start, end, _peak in _vision_clusters(vision_frames, interval, vthresh, min_len, max_len):
        candidates.append((start, end, None))

    fused: list[Moment] = []
    for start, end, tm in candidates:
        text_s = tm.score if tm else 0.0
        vframes = _vision_in(vision_frames, start, end)
        video_s = max((f.visual_score for f in vframes), default=0.0)
        audio_s = _max_audio(audio_energy, start, end)
        highlight = wA * audio_s + wT * text_s + wV * video_s

        if tm:
            category, title, reason = tm.category, tm.title, tm.reason
        else:
            category, label = _top_category(vframes, "action")
            title = label or category.replace("_", " ").title()
            reason = f"visual highlight ({label})" if label else "visual highlight"

        fused.append(Moment(
            start=round(start, 2), end=round(end, 2),
            category=category, score=round(highlight, 4),
            title=title, reason=reason,
            audio_score=round(audio_s, 4),
            text_score=round(text_s, 4),
            video_score=round(video_s, 4),
        ))

    fused.sort(key=lambda m: m.score, reverse=True)
    return _dedupe(fused)[:max_clips]


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


def save_moments(moments: list[Moment], path) -> None:
    path.write_text(
        json.dumps([m.to_dict() for m in moments], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
