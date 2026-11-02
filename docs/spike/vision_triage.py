#!/usr/bin/env python3
"""Spike 2: send grid frames to an Ollama vision model with the triage schema.

Mirrors vision.triage_frames as closely as possible (same system prompt, same
"index N:" markers, same JSON schema) but speaks Ollama /api/chat with
format=<schema> and message.images=[base64...]. Stdlib only.

Usage:
  python3 vision_triage.py --frames /path/to/frames --n 16 --runs 3 [--model qwen3-vl:8b]
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

CATEGORIES = ["action", "pvp", "explosion", "ship_damage", "landing", "bug"]

# == vision._FRAME_TOOL["input_schema"]
FRAME_SCHEMA = {
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
}

SYSTEM = (
    "You triage Star Citizen gameplay frames for short-form highlight potential. "
    f"For every frame return scores 0..1 for each category: {', '.join(CATEGORIES)}, "
    "plus visual_score (overall 0..1) and a short label. Be strict and consistent: "
    "menus, loading screens, inventory, quiet flight or empty space are 0.0-0.1; "
    "weapons fire, tracers, hull damage, fire, debris, crashes, near-misses, a dramatic "
    "approach or landing are 0.5+; a clear explosion, kill, boarding fight or crash "
    "in progress is 0.8+. Score each shown frame by its index. "
    "Return ONLY the JSON object."
)


def call(host: str, model: str, frames: list[tuple[int, Path]], num_ctx: int, timeout: int) -> tuple[dict, float]:
    text = "Score these frames. Each image is preceded by its frame index.\n"
    text += "\n".join(f"index {idx}:" for idx, _ in frames)
    images = [base64.b64encode(p.read_bytes()).decode("ascii") for _, p in frames]
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "format": FRAME_SCHEMA,
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text, "images": images},
        ],
    }
    req = urllib.request.Request(
        f"{host}/api/chat", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", required=True, help="directory with f_*.jpg")
    ap.add_argument("--n", type=int, default=16, help="frames per call (prod: max_frames_per_call=16)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default="qwen3-vl:8b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    all_frames = sorted(Path(args.frames).glob("f_*.jpg"))
    if not all_frames:
        print(f"no f_*.jpg in {args.frames}", file=sys.stderr)
        return 2
    print(f"model={args.model} frames/call={args.n} runs={args.runs} num_ctx={args.num_ctx}")
    print(f"total frames available: {len(all_frames)}; jpeg bytes of first: {all_frames[0].stat().st_size}")

    last_parsed = None
    for run in range(args.runs):
        # rotate through the pool so every run sees different images (no KV cache reuse)
        start = (run * args.n) % len(all_frames)
        pick = (all_frames * 2)[start:start + args.n]
        frames = [(start + i, p) for i, p in enumerate(pick)]
        try:
            data, wall = call(args.host, args.model, frames, args.num_ctx, args.timeout)
        except Exception as e:  # noqa: BLE001
            print(f"run {run + 1}: FAILED {type(e).__name__}: {e}")
            continue
        content = data.get("message", {}).get("content", "")
        pe = data.get("prompt_eval_count", 0)
        ped = data.get("prompt_eval_duration", 0) / 1e9
        ec = data.get("eval_count", 0)
        ed = data.get("eval_duration", 0) / 1e9
        load = data.get("load_duration", 0) / 1e9
        tok_s = ec / ed if ed else 0.0
        pp_s = pe / ped if ped else 0.0
        try:
            parsed = json.loads(content)
            ok = isinstance(parsed, dict) and isinstance(parsed.get("frames"), list)
            got = {f.get("index") for f in parsed.get("frames", []) if isinstance(f, dict)} if ok else set()
            want = {idx for idx, _ in frames}
            idx_ok = got == want
            last_parsed = parsed
        except json.JSONDecodeError as e:
            ok, idx_ok = False, False
            print(f"  JSONDecodeError: {e}; content[:300]={content[:300]!r}")
        print(
            f"run {run + 1}: wall={wall:6.1f}s load={load:5.1f}s | "
            f"prompt_tokens={pe} ({pp_s:.0f} tok/s prefill) | "
            f"gen_tokens={ec} ({tok_s:.1f} tok/s) | json_ok={ok} all_indices={idx_ok} "
            f"done_reason={data.get('done_reason')}"
        )
    if last_parsed is not None:
        print("\nexample response object (first 2 frames):")
        print(json.dumps({"frames": last_parsed.get("frames", [])[:2]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
