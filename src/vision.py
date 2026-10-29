"""Vision stage: two-tier frame scoring with Claude Vision.

Star Citizen highlights are often visual (explosions, PvP, ship damage, landings,
bugs) with no telling speech. This stage gives the fusion step a video signal
independent of the transcript.

Tier 1 (triage, cheap model): every grid frame (one per `interval_seconds`,
downscaled) gets a rough 0..1 score. Calls run concurrently.
Tier 2 (confirm, strong model): clusters of hot triage frames are re-examined
as a short BURST of 3 frames ~1.5s apart around the peak, so the model sees
motion and escalation instead of a single still. Confirmed scores replace the
triage scores inside the window; unconfirmed frames are discounted.

Caches: frames/ (+meta.json), vision_triage.json, vision.json (final).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from .config import env
from .models import VisionFrame
from .signals import clusters

_CACHE_VERSION = 2

_FRAME_TOOL = {
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

_BURST_TOOL = {
    "name": "report_moment_scores",
    "description": "Score each provided moment (a burst of consecutive frames).",
    "input_schema": {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "the moment index shown"},
                        "scores": {
                            "type": "object",
                            "description": "per-category strength 0..1",
                            "additionalProperties": {"type": "number"},
                        },
                        "visual_score": {"type": "number", "description": "overall 0..1"},
                        "label": {"type": "string", "description": "what happens, <10 words"},
                    },
                    "required": ["index", "scores", "visual_score", "label"],
                },
            }
        },
        "required": ["moments"],
    },
}


# ------------------------------------------------------------------ frames ----
def frames_ready(out_dir: Path, interval: float, width: int) -> bool:
    """True if the frame grid on disk was extracted with these parameters."""
    meta = out_dir / "meta.json"
    if not meta.exists() or not any(out_dir.glob("f_*.jpg")):
        return False
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
        return float(m.get("interval")) == float(interval) and int(m.get("width")) == int(width)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return False


def reset_frames(out_dir: Path) -> None:
    """Drop a stale grid (different interval/width) and its derived caches."""
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    # facecam.json is grid-independent (and may hold a manual UI pick) — keep it.
    for stale in ("vision_triage.json", "vision.json"):
        p = out_dir.parent / stale
        if p.exists():
            p.unlink()


def write_frames_meta(out_dir: Path, interval: float, width: int) -> None:
    (out_dir / "meta.json").write_text(
        json.dumps({"interval": interval, "width": width}), encoding="utf-8")


def frame_filter(interval: float, width: int) -> str:
    return f"fps=1/{interval},scale={width}:-2"


def extract_frames(source: Path, out_dir: Path, interval: float, width: int) -> list[tuple[int, float, Path]]:
    """Dump one frame per `interval` seconds, downscaled to `width`. Cached by meta."""
    if not frames_ready(out_dir, interval, width):
        reset_frames(out_dir)
        subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-i", str(source),
             "-vf", frame_filter(interval, width), "-q:v", "5",
             str(out_dir / "f_%05d.jpg")],
            check=True,
        )
        write_frames_meta(out_dir, interval, width)
    existing = sorted(out_dir.glob("f_*.jpg"))
    # ffmpeg fps filter emits the first frame at ~t=0, then every `interval`.
    return [(i, round(i * interval, 2), p) for i, p in enumerate(existing)]


def extract_burst(source: Path, t: float, out_dir: Path, *, width: int,
                  n: int = 3, step: float = 1.5) -> list[Path]:
    """n frames `step` seconds apart centred on t. Cached by filename."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"b_{int(round(t * 10)):07d}_w{int(width)}"
    existing = sorted(out_dir.glob(f"{tag}_*.jpg"))
    if len(existing) >= n:
        return existing[:n]
    start = max(0.0, t - step * (n - 1) / 2)
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-ss", f"{start:.2f}", "-i", str(source),
         "-t", f"{step * n:.2f}", "-vf", f"fps=1/{step},scale={width}:-2",
         "-frames:v", str(n), "-q:v", "4", str(out_dir / f"{tag}_%d.jpg")],
        check=True, capture_output=True,
    )
    return sorted(out_dir.glob(f"{tag}_*.jpg"))[:n]


# ----------------------------------------------------------------- claude ----
def _batches(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _img_block(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _clamp01(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _call_batches(
    client, *, model: str, system: str, tool: dict, batches: list[list[dict]],
    concurrency: int, meter=None, max_tokens: int = 2048,
) -> list[Any]:
    """Run one messages.create per batch concurrently; return the responses.

    A failed batch is logged and dropped (its frames simply get no score) so one
    transient API error does not throw away the batches that already succeeded.
    """
    lock = threading.Lock()

    def one(content: list[dict]):
        try:
            msg = client.messages.create(
                model=model, max_tokens=max_tokens, system=system, tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:  # noqa: BLE001
            print(f"[yellow]vision batch failed ({type(e).__name__}: {e}) — skipped")
            return None
        if meter is not None:
            with lock:
                meter.add(model, msg.usage)
        return msg

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(one, batches))
    ok = [r for r in results if r is not None]
    if batches and not ok:
        raise RuntimeError("every vision batch failed")
    return ok


def _tool_items(msg: Any, key: str) -> list[dict]:
    items: list[dict] = []
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            items.extend(block.input.get(key, []) or [])
    return items


def triage_frames(
    frames: list[tuple[int, float, Path]], *, model: str, categories: list[str],
    max_frames_per_call: int, concurrency: int, meter=None,
) -> list[VisionFrame]:
    """Tier 1: rough score for every grid frame."""
    if not frames:
        return []
    from anthropic import Anthropic
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    system = (
        "You triage Star Citizen gameplay frames for short-form highlight potential. "
        f"For every frame return scores 0..1 for each category: {', '.join(categories)}, "
        "plus visual_score (overall 0..1) and a short label. Be strict and consistent: "
        "menus, loading screens, inventory, quiet flight or empty space are 0.0-0.1; "
        "weapons fire, tracers, hull damage, fire, debris, crashes, near-misses, a dramatic "
        "approach or landing are 0.5+; a clear explosion, kill, boarding fight or crash "
        "in progress is 0.8+. Score each shown frame by its index."
    )
    ts_by_index = {idx: t for idx, t, _ in frames}
    batches: list[list[dict]] = []
    for chunk in _batches(frames, max_frames_per_call):
        content: list[dict] = [{"type": "text",
                                "text": "Score these frames. Each image is preceded by its frame index."}]
        for idx, _t, path in chunk:
            content.append({"type": "text", "text": f"index {idx}:"})
            content.append(_img_block(path))
        batches.append(content)

    out: list[VisionFrame] = []
    for msg in _call_batches(client, model=model, system=system, tool=_FRAME_TOOL,
                             batches=batches, concurrency=concurrency, meter=meter):
        for fr in _tool_items(msg, "frames"):
            idx = fr.get("index")
            if idx not in ts_by_index:
                continue
            raw = fr.get("scores", {}) or {}
            out.append(VisionFrame(
                t=ts_by_index[idx],
                scores={c: _clamp01(raw.get(c)) for c in categories},
                visual_score=_clamp01(fr.get("visual_score")),
                label=str(fr.get("label", ""))[:80],
            ))
    out.sort(key=lambda f: f.t)
    return out


def confirm_windows(
    source: Path, work: Path, peaks: list[float], *, model: str, categories: list[str],
    width: int, max_windows_per_call: int, concurrency: int, meter=None,
) -> dict[int, tuple[float, dict[str, float], str]]:
    """Tier 2: re-score each peak as a 3-frame burst. Returns {peak_index: (score, scores, label)}."""
    if not peaks:
        return {}
    burst_dir = work / "frames" / "bursts"
    bursts: list[tuple[int, list[Path]]] = []
    for i, t in enumerate(peaks):
        try:
            imgs = extract_burst(source, t, burst_dir, width=width)
        except subprocess.CalledProcessError:
            continue
        if imgs:
            bursts.append((i, imgs))
    if not bursts:
        return {}

    from anthropic import Anthropic
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    system = (
        "You judge candidate highlight moments from a Star Citizen Twitch stream. Each "
        "moment is a burst of consecutive frames about 1.5 seconds apart, in order. Judge "
        "the MOTION and escalation across the burst, not a single still: is something "
        "actually happening (fight, explosion, crash, dramatic manoeuvre, visible bug) that a "
        "viewer who did not watch the stream would find exciting in a 20-second clip? "
        f"Return scores 0..1 for each category: {', '.join(categories)}, an overall "
        "visual_score and a short label of what happens. Be strict: steady flight, menus, "
        "walking, talking to NPCs are below 0.3 even if the HUD is busy."
    )
    batches: list[list[dict]] = []
    for chunk in _batches(bursts, max_windows_per_call):
        content: list[dict] = [{"type": "text",
                                "text": "Score these moments. Each burst is preceded by its moment index."}]
        for idx, imgs in chunk:
            content.append({"type": "text", "text": f"moment {idx}:"})
            for p in imgs:
                content.append(_img_block(p))
        batches.append(content)

    out: dict[int, tuple[float, dict[str, float], str]] = {}
    for msg in _call_batches(client, model=model, system=system, tool=_BURST_TOOL,
                             batches=batches, concurrency=concurrency, meter=meter):
        for it in _tool_items(msg, "moments"):
            idx = it.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(peaks):
                continue
            raw = it.get("scores", {}) or {}
            out[idx] = (_clamp01(it.get("visual_score")),
                        {c: _clamp01(raw.get(c)) for c in categories},
                        str(it.get("label", ""))[:80])
    return out


# ----------------------------------------------------------------- facecam ----
_FACECAM_TOOL = {
    "name": "report_facecam",
    "description": "Report the location of the streamer's webcam/facecam overlay box.",
    "input_schema": {
        "type": "object",
        "properties": {
            "present": {"type": "boolean", "description": "true if a facecam overlay is visible"},
            "x": {"type": "number", "description": "left edge, fraction of width 0..1"},
            "y": {"type": "number", "description": "top edge, fraction of height 0..1"},
            "w": {"type": "number", "description": "box width, fraction of width 0..1"},
            "h": {"type": "number", "description": "box height, fraction of height 0..1"},
        },
        "required": ["present"],
    },
}


def detect_facecam(work: Path, model: str, meter=None, samples: int = 6) -> dict | None:
    """Ask Claude Vision to locate the facecam box (fractions), or None if absent.

    Reads the frames already extracted for the vision stage; picks a few spread
    across the VOD (the cam is a static overlay, so a small sample is plenty).
    Result cached in data/<vod>/facecam.json.
    """
    cache = work / "facecam.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return data or None  # {} cached => "no facecam", return None
        except (json.JSONDecodeError, OSError):
            pass

    imgs = sorted((work / "frames").glob("f_*.jpg"))
    if not imgs:
        return None
    step = max(1, len(imgs) // samples)
    picks = imgs[::step][:samples]

    from anthropic import Anthropic
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY", required=True))
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            "These frames are from ONE Twitch stream. A streamer webcam/facecam is "
            "usually a fixed rectangular overlay in a corner (a person on camera), "
            "distinct from the game. Locate that facecam box and return its bounding "
            "box as fractions of the frame (x,y = top-left corner, w,h = size). If no "
            "facecam/person overlay is present, set present=false."
        ),
    }]
    for p in picks:
        content.append(_img_block(p))

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=512,
            tools=[_FACECAM_TOOL],
            tool_choice={"type": "tool", "name": "report_facecam"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception:  # noqa: BLE001 — detection is best-effort; fall back to config
        return None
    if meter is not None:
        meter.add(model, msg.usage)

    box: dict | None = None
    for block in msg.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        inp = block.input
        if not inp.get("present"):
            break
        try:
            x = min(1.0, max(0.0, float(inp["x"])))
            y = min(1.0, max(0.0, float(inp["y"])))
            w = min(1.0, max(0.05, float(inp["w"])))
            h = min(1.0, max(0.05, float(inp["h"])))
        except (KeyError, TypeError, ValueError):
            break
        # clamp box to stay inside the frame
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)
        box = {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}
        break

    try:
        cache.write_text(json.dumps(box or {}), encoding="utf-8")
    except OSError:
        pass
    return box


# --------------------------------------------------------------------- run ----
def _load_frames_cache(path: Path, params: dict) -> list[VisionFrame] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("params") == params:
            return [VisionFrame(**f) for f in data["data"]]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        pass
    return None


def _save_frames_cache(path: Path, params: dict, frames: list[VisionFrame]) -> None:
    path.write_text(json.dumps({"params": params, "data": [f.__dict__ for f in frames]},
                               ensure_ascii=False), encoding="utf-8")


def run(source: Path, work: Path, cfg: dict, meter=None, log=print) -> list[VisionFrame]:
    v = cfg.get("vision", {})
    interval = float(v.get("interval_seconds", 10))
    width = int(v.get("frame_width", 640))
    categories = list(v.get("categories", ["action", "pvp", "explosion", "ship_damage", "landing", "bug"]))
    triage_model = v.get("triage_model") or v.get("model", "claude-haiku-4-5")
    confirm_model = v.get("model", "claude-sonnet-4-6")
    concurrency = int(v.get("concurrency", 4))
    burst = bool(v.get("burst", True))
    triage_thr = float(v.get("triage_threshold", 0.4))
    discount = float(v.get("unconfirmed_discount", 0.6))

    final_params = {"v": _CACHE_VERSION, "interval": interval, "width": width,
                    "triage_model": triage_model, "model": confirm_model,
                    "burst": burst, "triage_threshold": triage_thr,
                    "burst_width": int(v.get("burst_width", 768)),
                    "unconfirmed_discount": discount,
                    "categories": categories}
    cached = _load_frames_cache(work / "vision.json", final_params)
    if cached is not None:
        return cached

    frames = extract_frames(source, work / "frames", interval=interval, width=width)

    # ---- tier 1
    t_params = {"v": _CACHE_VERSION, "interval": interval, "width": width,
                "model": triage_model, "categories": categories}
    tri = _load_frames_cache(work / "vision_triage.json", t_params)
    if tri is None:
        tri = triage_frames(
            frames, model=triage_model, categories=categories,
            max_frames_per_call=int(v.get("max_frames_per_call", 16)),
            concurrency=concurrency, meter=meter,
        )
        _save_frames_cache(work / "vision_triage.json", t_params, tri)
    log(f"  triage: {len(tri)} frames, {sum(1 for f in tri if f.visual_score >= triage_thr)} hot")

    if not burst:
        _save_frames_cache(work / "vision.json", final_params, tri)
        return tri

    # ---- tier 2: bursts around each hot cluster's peak
    hot = clusters([(f.t, f.visual_score) for f in tri], interval, triage_thr)
    peaks = [peak_t for _s, _e, peak_t, _ps in hot]
    confirmed = confirm_windows(
        source, work, peaks, model=confirm_model, categories=categories,
        width=int(v.get("burst_width", 768)),
        max_windows_per_call=int(v.get("max_windows_per_call", 4)),
        concurrency=concurrency, meter=meter,
    )
    log(f"  confirm: {len(peaks)} bursts, {len(confirmed)} scored")

    by_t = {f.t: f for f in tri}
    final: list[VisionFrame] = []
    in_window: set[float] = set()
    for i, (s, e, peak_t, _ps) in enumerate(hot):
        res = confirmed.get(i)
        for f in tri:
            if s <= f.t < e:
                in_window.add(f.t)
                if res is None:
                    final.append(VisionFrame(f.t, f.scores, round(f.visual_score * discount, 4), f.label))
                elif f.t == peak_t:
                    final.append(VisionFrame(f.t, res[1], round(res[0], 4), res[2]))
                else:
                    final.append(VisionFrame(f.t, f.scores, round(min(f.visual_score, res[0]), 4), f.label))
    for f in tri:
        if f.t not in in_window:
            final.append(VisionFrame(f.t, f.scores, round(f.visual_score * discount, 4), f.label))
    final.sort(key=lambda f: f.t)
    _save_frames_cache(work / "vision.json", final_params, final)
    return final
