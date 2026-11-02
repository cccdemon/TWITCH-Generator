#!/usr/bin/env python3
"""Spike 3: large refine-style prompt against an Ollama text model.

Builds a synthetic refine prompt (30 candidates, each with ~45 transcript lines of
German stream chatter) sized to --target-tokens, sends it with format=<refine
schema>, think=false, temperature=0 and the given num_ctx, then reports whether
the prompt was truncated (prompt_eval_count far below the estimate), prefill and
generation speed and JSON validity. Stdlib only.

Usage:
  python3 text_ctx.py --target-tokens 25000 --num-ctx 32768 [--model qwen3:14b]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request

# == refine._TOOL["input_schema"]
CLIP_SCHEMA = {
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
}

CATEGORIES = ["funny", "emotional", "exciting", "skillful",
              "action", "pvp", "explosion", "ship_damage", "landing", "bug"]

RUBRIC = (
    "Scoring rubric (apply the SAME scale to every candidate):\n"
    "  0.9-1.0  would stop a scroll cold: huge laugh, insane play, raw emotion, chat exploded\n"
    "  0.7-0.8  clearly clip-worthy, obvious payoff within the window\n"
    "  0.5-0.6  decent but needs context or the payoff is mild\n"
    "  <0.5     filler, setup without payoff, or the signals are noise — DROP IT\n"
)

SYSTEM = (
    "You are the final editor for short-form clips (TikTok/Shorts/Reels) cut from a "
    "Twitch stream. You get candidate windows found by chat spikes, loudness bursts, a "
    "transcript scan and a video scan, each with transcript context. Pick the best "
    "at most 10. Rules:\n"
    "- Each clip is 15-30s. The hook must land in the first 3 seconds; "
    "end right after the payoff (laugh, kill, crash, reaction), never mid-sentence.\n"
    "- Prefer windows where several signals agree (chat + audio + video).\n"
    "- Chat reacts a few seconds AFTER the moment; the moment is just before the chat peak.\n"
    "- Use only timestamps that appear in the transcript lines or the window.\n"
    "- Categories: " + ", ".join(CATEGORIES) + ".\n"
    "- Captions in the language of the stream, punchy, no hashtags.\n"
    + RUBRIC
    + "It is fine to return fewer clips, or none, if nothing clears 0.5.\n"
    "Return ONLY the JSON object."
)

WORDS = (
    "okay also wir fliegen jetzt mal rüber zu der Station und schauen ob da jemand ist "
    "ne warte warte warte was war das da hinten ich glaub der schießt auf uns Alter "
    "nein nein nein Schilde sind unten Schilde sind unten ich kann nicht ausweichen "
    "boah das Ding explodiert gleich raus raus raus hahaha ich fass es nicht "
    "Chat habt ihr das gesehen ich hab den mit dem Railgun Schuss einfach weggeholt "
    "Moment der Server lagt schon wieder ich falle durch den Boden klassischer Bug "
    "lass uns kurz landen und das Schiff reparieren dann machen wir die Bounty "
    "ich hab keine Munition mehr wieso hab ich keine Munition mehr das kann nicht sein "
    "ja genau so machen wir das Leute danke für die Subs ihr seid der Wahnsinn heute"
).split()


def fake_transcript(rng: random.Random, t0: float, lines: int) -> str:
    out = []
    t = t0
    for _ in range(lines):
        n = rng.randint(6, 16)
        dur = rng.uniform(2.0, 6.0)
        txt = " ".join(rng.choice(WORDS) for _ in range(n))
        out.append(f"[{t:.0f}-{t + dur:.0f}] {txt}")
        t += dur + rng.uniform(0.2, 1.5)
    return "\n".join(out)


def build_prompt(target_tokens: int, chars_per_token: float = 3.3) -> tuple[str, int]:
    rng = random.Random(7731)
    n_cand = 30
    lines = 10
    while True:
        blocks = []
        for i in range(n_cand):
            s = 600 + i * 700 + rng.uniform(0, 300)
            e = s + rng.uniform(18, 30)
            blocks.append(
                f"### candidate {i}\n"
                f"window: {s:.0f}-{e:.0f}s  fused={rng.uniform(0.3, 0.9):.2f}  "
                f"(chat={rng.random():.2f} audio={rng.random():.2f} "
                f"text={rng.random():.2f} video={rng.random():.2f})\n"
                f"category guess: {rng.choice(CATEGORIES)}; first-pass note: {' '.join(rng.choice(WORDS) for _ in range(8))}\n"
                f"hints: vision={rng.choice(['explosion', 'pvp', 'landing', '-'])}; chat peak at {s + 8:.0f}s\n"
                f"transcript (with context):\n{fake_transcript(rng, s - 20, lines)}\n"
            )
        prompt = "\n".join(blocks) + "\n\nReturn the final selection via finalize_clips."
        est = int(len(prompt) / chars_per_token)
        if est >= target_tokens or lines > 400:
            return prompt, est
        lines += 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-tokens", type=int, default=25000)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    prompt, est = build_prompt(args.target_tokens)
    print(f"model={args.model} num_ctx={args.num_ctx} prompt_chars={len(prompt)} est_prompt_tokens≈{est} (len/3.3)")
    body = {
        "model": args.model,
        "stream": False,
        "think": False,
        "format": CLIP_SCHEMA,
        "options": {"temperature": 0, "num_ctx": args.num_ctx, "num_predict": 4096},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        f"{args.host}/api/chat", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"FAILED {type(e).__name__}: {e}")
        return 1
    wall = time.time() - t0
    pe = data.get("prompt_eval_count", 0)
    ped = data.get("prompt_eval_duration", 0) / 1e9
    ec = data.get("eval_count", 0)
    ed = data.get("eval_duration", 0) / 1e9
    load = data.get("load_duration", 0) / 1e9
    content = data.get("message", {}).get("content", "")
    ratio = pe / est if est else 0
    verdict = "OK (no truncation)" if ratio > 0.8 else "SUSPECT: prompt_eval_count far below estimate → truncated or cached"
    print(
        f"wall={wall:.1f}s load={load:.1f}s | prompt_eval_count={pe} ({pe / ped if ped else 0:.0f} tok/s prefill, {ped:.1f}s) | "
        f"gen={ec} tokens ({ec / ed if ed else 0:.1f} tok/s, {ed:.1f}s) | done_reason={data.get('done_reason')}"
    )
    print(f"prompt_eval_count/estimate = {ratio:.2f} → {verdict}")
    try:
        parsed = json.loads(content)
        clips = parsed.get("clips", [])
        idx = [c.get("index") for c in clips]
        print(f"json_ok=True clips={len(clips)} indices={idx}")
        print(json.dumps(clips[:2], indent=2, ensure_ascii=False))
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"json_ok=False ({e}); content[:400]={content[:400]!r}")
    print("\nnow run:  docker exec ollama ollama ps   (SIZE + PROCESSOR column) and")
    print("          docker logs ollama 2>&1 | grep -i -E 'truncat|exceed|offload' | tail -5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
