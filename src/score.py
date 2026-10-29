"""Fusion stage: combine chat + audio + text + video signals into ranked candidates.

    highlight = wC*chat + wA*audio + wT*text + wV*video     (weights normalised)

Candidate windows come from three sources so no signal has veto power:
  - transcript moments (text),
  - clusters of high-scoring vision frames (silent visual moments),
  - clusters of chat hype (the audience already clipped it in their heads).
Audio alone never spawns a candidate (too noisy) but boosts the others.

Each candidate is then scored against ALL signals over its window and the top
`max_clips * candidate_factor` go to the refine pass, which picks the final set
and fixes the boundaries with full transcript context.
"""
from __future__ import annotations

import json

from .models import Moment, VisionFrame
from .signals import Signal, clusters, max_in, peak_in


def _norm_weights(w: dict, present: dict[str, bool] | None = None) -> tuple[float, float, float, float]:
    """Normalised (chat, audio, text, video) weights. Signals absent for this run
    (disabled or empty) get weight 0 so the scale stays 0..1 either way."""
    present = present or {}
    raw = {"chat": 0.35, "audio": 0.15, "text": 0.25, "video": 0.25}
    vals = {k: float(w.get(k, d)) if present.get(k, True) else 0.0 for k, d in raw.items()}
    s = sum(vals.values()) or 1.0
    return vals["chat"] / s, vals["audio"] / s, vals["text"] / s, vals["video"] / s


def _window_around(start: float, end: float, *, lead: float,
                   min_len: float, max_len: float) -> tuple[float, float]:
    """Pad a detected cluster into a clip-sized window: a short lead-in before
    the action, then enough aftermath to reach min_len, capped at max_len."""
    s = max(0.0, start - lead)
    e = max(end, s + min_len)
    if e - s > max_len:
        e = s + max_len
    return round(s, 2), round(e, 2)


def _vision_in(frames: list[VisionFrame], start: float, end: float) -> list[VisionFrame]:
    return [f for f in frames if start <= f.t <= end]


def _top_category(frames: list[VisionFrame], fallback: str) -> tuple[str, str]:
    """Dominant vision category + label across the given frames."""
    if not frames:
        return fallback, ""
    peak = max(frames, key=lambda f: f.visual_score)
    cat = max(peak.scores, key=peak.scores.get) if peak.scores else fallback
    return cat, peak.label


def _vision_signal(frames: list[VisionFrame]) -> Signal:
    return [(f.t, f.visual_score) for f in frames]


def fuse(
    text_moments: list[Moment],
    vision_frames: list[VisionFrame],
    audio: Signal,
    chat: Signal,
    cfg: dict,
) -> list[Moment]:
    score_cfg = cfg.get("score", {})
    wC, wA, wT, wV = _norm_weights(score_cfg.get("weights", {}), {
        "chat": bool(chat), "audio": bool(audio),
        "text": bool(text_moments), "video": bool(vision_frames),
    })
    a_cfg = cfg.get("analyze", {})
    v_cfg = cfg.get("vision", {})
    c_cfg = cfg.get("chat", {})
    s_cfg = cfg.get("signals", {})
    min_len = float(a_cfg.get("min_clip_seconds", 15))
    max_len = float(a_cfg.get("max_clip_seconds", 60))
    lead = float(score_cfg.get("lead_seconds", 5))
    max_clips = int(a_cfg.get("max_clips", 10))
    factor = int(score_cfg.get("candidate_factor", 3))

    v_interval = float(v_cfg.get("interval_seconds", 10))
    v_thresh = float(v_cfg.get("highlight_threshold", 0.5))
    s_interval = float(s_cfg.get("interval_seconds", 10))
    c_thresh = float(c_cfg.get("highlight_threshold", 0.5))

    # ---- candidate windows: (start, end, text_moment | None, origin)
    candidates: list[tuple[float, float, Moment | None, str]] = []
    for m in text_moments:
        candidates.append((m.start, m.end, m, "text"))
    for cs, ce, _pt, _ps in clusters(_vision_signal(vision_frames), v_interval, v_thresh):
        s, e = _window_around(cs, ce, lead=lead, min_len=min_len, max_len=max_len)
        candidates.append((s, e, None, "video"))
    for cs, ce, _pt, _ps in clusters(chat, s_interval, c_thresh):
        s, e = _window_around(cs, ce, lead=lead, min_len=min_len, max_len=max_len)
        candidates.append((s, e, None, "chat"))

    fused: list[Moment] = []
    for start, end, tm, origin in candidates:
        text_s = tm.score if tm else 0.0
        vframes = _vision_in(vision_frames, start, end)
        video_s = max((f.visual_score for f in vframes), default=0.0)
        audio_s = max_in(audio, start, end)
        chat_s = max_in(chat, start, end)
        highlight = wC * chat_s + wA * audio_s + wT * text_s + wV * video_s

        vcat, vlabel = _top_category(vframes, "action")
        if tm:
            category, title, reason = tm.category, tm.title, tm.reason
        else:
            category = vcat if origin == "video" or video_s >= v_thresh else "exciting"
            title = vlabel or category.replace("_", " ").title()
            reason = f"{origin} spike" + (f" ({vlabel})" if vlabel else "")

        hints: list[str] = [f"origin={origin}"]
        if vlabel:
            hints.append(f"vision: {vlabel}")
        cp = peak_in(chat, start, end)
        if cp and cp[1] > 0:
            hints.append(f"chat peak {cp[1]:.2f} at {cp[0]:.0f}s")
        ap = peak_in(audio, start, end)
        if ap and ap[1] > 0:
            hints.append(f"audio burst {ap[1]:.2f} at {ap[0]:.0f}s")

        fused.append(Moment(
            start=round(start, 2), end=round(end, 2),
            category=category, score=round(highlight, 4),
            title=title, reason=reason,
            audio_score=round(audio_s, 4), text_score=round(text_s, 4),
            video_score=round(video_s, 4), chat_score=round(chat_s, 4),
            hints="; ".join(hints),
        ))

    fused.sort(key=lambda m: m.score, reverse=True)
    return _dedupe(fused)[: max(max_clips, max_clips * factor)]


def _overlap(a: Moment, b: Moment) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shorter = min(a.end - a.start, b.end - b.start) or 1.0
    return inter / shorter


def _dedupe(moments: list[Moment]) -> list[Moment]:
    """Drop moments whose windows heavily overlap an already-kept higher-scored one.
    The survivor absorbs the loser's component scores (max) so a text moment that
    also had a chat spike keeps both facts."""
    kept: list[Moment] = []
    for m in moments:
        hit = next((k for k in kept if _overlap(m, k) > 0.5), None)
        if hit is not None:
            hit.chat_score = max(hit.chat_score, m.chat_score)
            hit.audio_score = max(hit.audio_score, m.audio_score)
            hit.video_score = max(hit.video_score, m.video_score)
            hit.text_score = max(hit.text_score, m.text_score)
            continue
        kept.append(m)
    return kept


def save_moments(moments: list[Moment], path) -> None:
    path.write_text(
        json.dumps([m.to_dict() for m in moments], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
