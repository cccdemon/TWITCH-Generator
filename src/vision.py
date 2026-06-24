"""Vision stage: sample frames every N seconds and let Claude Vision score them.

Star Citizen highlights are often visual (explosions, PvP, ship damage, landings,
bugs) with no telling speech. This stage gives the fusion step a video signal that
is independent of the transcript.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

from anthropic import Anthropic

from .config import env
from .models import VisionFrame

_TOOL = {
    "name": "report_frame_scores",
    "description": "Score each provided game frame for short-form highlight potential.",
    "input_schema": {
        "type": "object",
        "properties": {
            "frames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "the frame index shown"},
                        "scores": {
                            "type": "object",
                            "description": "per-category strength 0..1",
                            "additionalProperties": {"type": "number"},
                        },
                        "visual_score": {"type": "number", "description": "overall 0..1"},
                        "label": {"type": "string", "description": "short description"},
                    },
                    "required": ["index", "scores", "visual_score", "label"],
                },
            }
        },
        "required": ["frames"],
    },
}


def extract_frames(source: Path, out_dir: Path, interval: float, width: int) -> list[tuple[int, float, Path]]:
    """Dump one frame per `interval` seconds, downscaled to `width`. Cached by presence."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("f_*.jpg"))
    if not existing:
        cmd = [
            "ffmpeg", "-y", "-i", str(source),
            "-vf", f"fps=1/{interval},scale={width}:-2",
            "-q:v", "5",
            str(out_dir / "f_%05d.jpg"),
        ]
        subprocess.run(cmd, check=True)
        existing = sorted(out_dir.glob("f_*.jpg"))
    # ffmpeg fps filter emits the first frame at ~t=0, then every `interval`.
    return [(i, round(i * interval, 2), p) for i, p in enumerate(existing)]


def _batches(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _img_block(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def score_frames(
    frames: list[tuple[int, float, Path]],
    model: str,
    categories: list[str],
    max_frames_per_call: int,
) -> list[VisionFrame]:
    if not frames:
        return []
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    system = (
        "You score Star Citizen gameplay frames for short-form highlight potential. "
        f"For every frame return scores 0..1 for each category: {', '.join(categories)}. "
        "Also return visual_score (overall 0..1) and a short label. Be strict: a calm "
        "menu/loading/empty-space frame is ~0; a clear explosion, dogfight, crash or "
        "dramatic landing is high. Score each shown frame by its index."
    )

    ts_by_index = {idx: t for idx, t, _ in frames}
    out: list[VisionFrame] = []
    for chunk in _batches(frames, max_frames_per_call):
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": "Score these frames. Each image is preceded by its frame index.",
        }]
        for idx, _t, path in chunk:
            content.append({"type": "text", "text": f"index {idx}:"})
            content.append(_img_block(path))

        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "report_frame_scores"},
            messages=[{"role": "user", "content": content}],
        )
        out.extend(_parse(msg, ts_by_index, categories))

    out.sort(key=lambda f: f.t)
    return out


def _parse(msg: Any, ts_by_index: dict[int, float], categories: list[str]) -> list[VisionFrame]:
    res: list[VisionFrame] = []
    for block in msg.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        for fr in block.input.get("frames", []):
            idx = fr.get("index")
            if idx not in ts_by_index:
                continue
            raw = fr.get("scores", {}) or {}
            scores = {c: max(0.0, min(1.0, float(raw.get(c, 0.0) or 0.0))) for c in categories}
            res.append(VisionFrame(
                t=ts_by_index[idx],
                scores=scores,
                visual_score=max(0.0, min(1.0, float(fr.get("visual_score", 0.0) or 0.0))),
                label=str(fr.get("label", ""))[:80],
            ))
    return res


def run(source: Path, work: Path, cfg: dict) -> list[VisionFrame]:
    v = cfg.get("vision", {})
    cache = work / "vision.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return [VisionFrame(**f) for f in data]

    frames = extract_frames(
        source, work / "frames",
        interval=v.get("interval_seconds", 30),
        width=v.get("frame_width", 768),
    )
    result = score_frames(
        frames,
        model=v.get("model", "claude-sonnet-4-6"),
        categories=v.get("categories", ["action", "pvp", "explosion", "ship_damage", "landing", "bug"]),
        max_frames_per_call=v.get("max_frames_per_call", 12),
    )
    cache.write_text(
        json.dumps([f.__dict__ for f in result], ensure_ascii=False),
        encoding="utf-8",
    )
    return result
