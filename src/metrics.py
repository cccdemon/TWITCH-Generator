"""Live run metrics: data/<vod_id>/metrics.json, written by the pipeline process.

The web UI (job page) polls /api/jobs/{id}/metrics, which reads this file and
adds Ollama's /api/ps. Everything here is best-effort: a metrics failure must
never break the pipeline, so all writers swallow OSError and the module works
as a no-op when no run is active (CLI imports, unit tests).

Fields (all optional for readers):
  stage, stages{name: {start, end}}, started, updated
  llm{calls, input_tokens, output_tokens, usd, gen_tok_s_avg, prefill_tok_s_avg,
      last{model, prompt_tokens, gen_tokens, gen_tok_s, prefill_tok_s, wall_s, at},
      by_model{model: {calls, input_tokens, output_tokens}}}
  whisper{backend, model, chunks_total, chunks_done, audio_s, wall_s, factor,
          last{audio_s, wall_s, factor}}
  vision{frames, hot, bursts, confirmed}
  render{done, total}
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_FLUSH_MIN_INTERVAL = 1.0


class Metrics:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_flush = 0.0
        now = time.time()
        self.data: dict = {
            "v": 1,
            "started": now,
            "updated": now,
            "stage": None,
            "stages": {},
            "llm": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0,
                    "gen_tok_s_avg": None, "prefill_tok_s_avg": None, "last": None,
                    "by_model": {}, "_gen_tokens_timed": 0, "_gen_seconds": 0.0,
                    "_prompt_tokens_timed": 0, "_prefill_seconds": 0.0},
            "whisper": {"backend": None, "model": None, "chunks_total": 0, "chunks_done": 0,
                        "audio_s": 0.0, "wall_s": 0.0, "factor": None, "last": None},
            "vision": {},
            "render": {"done": 0, "total": 0},
        }
        self.flush(force=True)

    # ------------------------------------------------------------ writers --
    def stage(self, name: str) -> None:
        now = time.time()
        with self._lock:
            cur = self.data.get("stage")
            if cur and cur in self.data["stages"] and not self.data["stages"][cur].get("end"):
                self.data["stages"][cur]["end"] = now
            self.data["stage"] = name
            self.data["stages"].setdefault(name, {"start": now, "end": None})
        self.flush(force=True)

    def llm_call(self, *, model: str, prompt_tokens: int, gen_tokens: int, wall_s: float,
                 prefill_s: float | None = None, gen_s: float | None = None, usd: float | None = None) -> None:
        with self._lock:
            l = self.data["llm"]
            l["calls"] += 1
            l["input_tokens"] += int(prompt_tokens)
            l["output_tokens"] += int(gen_tokens)
            if usd is not None:
                l["usd"] = round(float(usd), 4)
            bm = l["by_model"].setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            bm["calls"] += 1
            bm["input_tokens"] += int(prompt_tokens)
            bm["output_tokens"] += int(gen_tokens)
            # Ollama reports eval_duration; the Anthropic path only knows wall time.
            gs = gen_s if gen_s else wall_s
            gen_tok_s = round(gen_tokens / gs, 1) if gs and gen_tokens else None
            prefill_tok_s = round(prompt_tokens / prefill_s, 0) if prefill_s and prompt_tokens else None
            if gen_tok_s is not None:
                l["_gen_tokens_timed"] += int(gen_tokens)
                l["_gen_seconds"] += float(gs)
                l["gen_tok_s_avg"] = round(l["_gen_tokens_timed"] / l["_gen_seconds"], 1) if l["_gen_seconds"] else None
            if prefill_tok_s is not None:
                l["_prompt_tokens_timed"] += int(prompt_tokens)
                l["_prefill_seconds"] += float(prefill_s)
                l["prefill_tok_s_avg"] = round(l["_prompt_tokens_timed"] / l["_prefill_seconds"], 0) if l["_prefill_seconds"] else None
            l["last"] = {"model": model, "prompt_tokens": int(prompt_tokens), "gen_tokens": int(gen_tokens),
                         "gen_tok_s": gen_tok_s, "prefill_tok_s": prefill_tok_s,
                         "wall_s": round(wall_s, 1), "at": time.time()}
        self.flush()

    def whisper_start(self, *, backend: str, model: str, chunks_total: int) -> None:
        with self._lock:
            w = self.data["whisper"]
            w.update({"backend": backend, "model": model, "chunks_total": int(chunks_total)})
        self.flush(force=True)

    def whisper_chunk(self, *, audio_s: float, wall_s: float, done: int) -> None:
        with self._lock:
            w = self.data["whisper"]
            w["chunks_done"] = int(done)
            w["audio_s"] += float(audio_s)
            w["wall_s"] += float(wall_s)
            w["factor"] = round(w["audio_s"] / w["wall_s"], 1) if w["wall_s"] else None
            w["last"] = {"audio_s": round(audio_s), "wall_s": round(wall_s, 1),
                         "factor": round(audio_s / wall_s, 1) if wall_s else None}
        self.flush(force=True)

    def vision(self, **fields) -> None:
        with self._lock:
            self.data["vision"].update(fields)
        self.flush(force=True)

    def render(self, done: int, total: int) -> None:
        with self._lock:
            self.data["render"] = {"done": int(done), "total": int(total)}
        self.flush(force=True)

    # -------------------------------------------------------------- flush --
    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_flush < _FLUSH_MIN_INTERVAL:
            return
        with self._lock:
            self.data["updated"] = now
            payload = json.dumps(self.data, ensure_ascii=False)
            self._last_flush = now
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


# ------------------------------------------------------------- process-global --
_current: Metrics | None = None


def start(work: Path) -> Metrics:
    global _current
    _current = Metrics(work / "metrics.json")
    return _current


def current() -> Metrics | None:
    return _current


def load(work: Path) -> dict | None:
    p = work / "metrics.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # strip accumulators the UI does not need
    for k in [k for k in d.get("llm", {}) if k.startswith("_")]:
        d["llm"].pop(k, None)
    return d
