"""Phone posting: caption suggestion, platform limits, per-clip state (posts.json).

Pure logic, no web or ffmpeg imports at module level, so it is testable on its own.
The phone itself is driven by the separate clip-poster service (poster/), reached
through src/poster_client.py; the routes live in src/posting_web.py.

posts.json = {clip_name: {"trim": {"mode", "start", "end"} | None,
                          "caption": str | None, "ai_tags": [str] | None,
                          "posts": [{"id", "platforms", "dry_run", "run_at", "created", "status", ...}]}}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

MARGIN_S = 20.0
LIMITS = {"instagram": {"chars": 2200, "hashtags": 30}, "tiktok": {"chars": 4000, "hashtags": None}}
DEFAULT_TEMPLATE = "{title}\n\nLive: {twitch}\n\n{hashtags}"
ACTIVE = {"queued", "running"}

_LINK = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:tv|com|de|gg|net|org|io)(?:/\S*)?")
_LABEL = {"instagram": "Instagram", "tiktok": "TikTok"}


# ------------------------------------------------------------------ caption
def normalize_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        body = re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if body:
            out.append("#" + body)
    return out


def merge_hashtags(fixed: list[str], ai: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for t in normalize_tags(fixed) + normalize_tags(ai):
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def parse_fixed_tags(raw: str) -> list[str]:
    return normalize_tags([p for p in re.split(r"[\s,]+", raw or "") if p])


def build_caption(template: str, title: str, twitch: str, hashtags: list[str]) -> str:
    tpl = (template or DEFAULT_TEMPLATE).replace("\\n", "\n")
    if not (twitch or "").strip():
        tpl = "\n".join(line for line in tpl.split("\n") if "{twitch}" not in line)
    text = (tpl.replace("{title}", title or "")
               .replace("{twitch}", (twitch or "").strip())
               .replace("{hashtags}", " ".join(hashtags)))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def check_caption(caption: str) -> dict:
    chars = len(caption)
    tags = caption.count("#")
    res = {"chars": chars, "hashtags": tags, "links": len(_LINK.findall(caption)),
           "ok": {}, "errors": {}}
    for platform, lim in LIMITS.items():
        err = ""
        if chars > lim["chars"]:
            err = f"{_LABEL[platform]}: Text länger als {lim['chars']} Zeichen"
        elif lim["hashtags"] is not None and tags > lim["hashtags"]:
            err = f"{_LABEL[platform]}: mehr als {lim['hashtags']} Hashtags"
        res["ok"][platform] = not err
        if err:
            res["errors"][platform] = err
    return res


_HASHTAG_SCHEMA = {
    "type": "object",
    "properties": {"hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
    "required": ["hashtags"],
}
_HASHTAG_SYSTEM = (
    "Du schlägst Hashtags für einen kurzen Gaming-Clip (TikTok/Instagram Reels) vor. "
    "Gib 3 bis 5 passende, gängige Hashtags zurück, die zum Inhalt dieses Clips passen. "
    "Keine Hashtags wiederholen, die schon in der festen Liste stehen. Nur das Wort, ohne Erklärung."
)


def suggest_hashtags(title: str, category: str, transcript_excerpt: str, fixed: list[str],
                     cfg: dict, log=print) -> list[str]:
    """AI hashtags for one clip. Never raises: any failure → [] (fixed tags still apply)."""
    from . import llm, pricing

    try:
        meter = pricing.CostMeter()
        text = (f"Titel: {title}\nKategorie: {category}\nFeste Hashtags: {' '.join(fixed)}\n"
                f"Gesprochenes im Clip:\n{transcript_excerpt[:1500]}")
        out = llm.structured(
            role="text", system=_HASHTAG_SYSTEM, content=[llm.TextPart(text)],
            schema=_HASHTAG_SCHEMA, tool_name="hashtags", cfg=cfg, meter=meter,
            max_tokens=300, model=(cfg.get("refine") or {}).get("model"), log=log,
        )
        log(f"posting: hashtags for {title!r} (${meter.usd:.4f})")
        return normalize_tags(out.get("hashtags") or [])[:5]
    except Exception as e:  # noqa: BLE001 — a hashtag hint must never block posting
        log(f"posting: hashtag suggestion failed: {e}")
        return []


# -------------------------------------------------------------------- clips
def clip_kind(name: str, moments: dict[int, dict]) -> str:
    """"pipeline" if the clip maps to a moment (leading start second), else "edit"."""
    lead = name.split("_", 1)[0]
    if "_edit_" in name or not lead.isdigit():
        return "edit"
    return "pipeline" if int(lead) in moments else "edit"


# -------------------------------------------------------------------- store
_store_lock = threading.Lock()


def _empty() -> dict:
    return {"trim": None, "caption": None, "ai_tags": None, "posts": []}


def is_locked(entry: dict) -> bool:
    return any(p.get("status") in ACTIVE for p in entry.get("posts") or [])


class PostStore:
    def __init__(self, vod_dir: Path):
        self.path = Path(vod_dir) / "posts.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> dict:
        with _store_lock:
            return self._load()

    def get(self, clip: str) -> dict:
        with _store_lock:
            return {**_empty(), **(self._load().get(clip) or {})}

    def update(self, clip: str, **fields) -> dict:
        with _store_lock:
            data = self._load()
            entry = {**_empty(), **(data.get(clip) or {}), **fields}
            data[clip] = entry
            self._save(data)
            return entry

    def add_post(self, clip: str, rec: dict) -> None:
        with _store_lock:
            data = self._load()
            entry = {**_empty(), **(data.get(clip) or {})}
            entry["posts"] = list(entry["posts"]) + [rec]
            data[clip] = entry
            self._save(data)


# --------------------------------------------------------------------- trim
class LockedError(Exception):
    """A post of this clip is queued/running; the phone already has its copy."""


MIN_LEN_S = 1.0


def _vod_duration(vod_dir: Path) -> float | None:
    try:
        d = float(json.loads((vod_dir / "info.json").read_text(encoding="utf-8")).get("duration") or 0)
        return d or None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _source(vod_dir: Path) -> Path:
    return vod_dir / f"{vod_dir.name}.mp4"


def _orig_window(moment: dict, cfg: dict) -> tuple[float, float]:
    c = cfg.get("clip") or {}
    start = max(0.0, float(moment["start"]) - float(c.get("pad_start", 1.0)))
    return start, float(moment["end"]) + float(c.get("pad_end", 1.0))


def _moment(clip: str, moments: dict[int, dict]) -> dict:
    return moments[int(clip.split("_", 1)[0])]


def window(vod_dir: Path, clip: str, moments: dict[int, dict], cfg: dict) -> dict:
    """Current cut + draggable range for the timeline.

    mode "source": absolute VOD seconds, re-cut from the source video (pipeline
    clip and source still on disk). mode "clip": seconds within the clip file,
    shorten only.
    """
    from . import clip as clip_stage

    vod_dir = Path(vod_dir)
    kind = clip_kind(clip, moments)
    if kind == "pipeline" and _source(vod_dir).exists():
        o_start, o_end = _orig_window(_moment(clip, moments), cfg)
        trim = PostStore(vod_dir).get(clip)["trim"]
        if trim and trim.get("mode") == "source":
            start, end = float(trim["start"]), float(trim["end"])
        else:
            start, end = o_start, o_end
        lo = max(0.0, start - MARGIN_S)
        hi = end + MARGIN_S
        dur = _vod_duration(vod_dir)
        if dur:
            hi = min(hi, dur)
        return {"kind": kind, "mode": "source", "source": True, "start": start, "end": end,
                "min": lo, "max": hi, "orig_start": o_start, "orig_end": o_end}
    length = clip_stage.probe_duration(vod_dir / "clips" / clip)
    return {"kind": kind, "mode": "clip", "source": _source(vod_dir).exists(), "start": 0.0,
            "end": length, "min": 0.0, "max": length, "orig_start": 0.0, "orig_end": length}


def _render_source(vod_dir: Path, clip: str, start: float, end: float, moments, cfg,
                   facecam, segments) -> None:
    from . import clip as clip_stage

    c = cfg.get("clip") or {}
    clip_stage.render_custom(
        _source(vod_dir), vod_dir / "clips" / clip,
        start=start, duration=end - start,
        width=c.get("width", 1080), height=c.get("height", 1920),
        layout=c.get("layout", "split"), facecam=facecam, game=None,
        segments=segments, burn_subtitles=bool(c.get("burn_subtitles", False)),
        title=_moment(clip, moments).get("title", ""),
    )


def apply_trim(vod_dir: Path, clip: str, start: float, end: float, moments: dict[int, dict],
               cfg: dict, facecam: dict | None, segments) -> dict:
    from . import clip as clip_stage

    vod_dir = Path(vod_dir)
    store = PostStore(vod_dir)
    if is_locked(store.get(clip)):
        raise LockedError("erst Post abbrechen")
    w = window(vod_dir, clip, moments, cfg)
    start, end = float(start), float(end)
    if end - start < MIN_LEN_S:
        raise ValueError(f"Clip muss mindestens {MIN_LEN_S:g} s lang sein")
    if start < w["min"] - 0.01 or end > w["max"] + 0.01:
        raise ValueError("Schnitt außerhalb des verfügbaren Bereichs")
    if w["mode"] == "source":
        _render_source(vod_dir, clip, start, end, moments, cfg, facecam, segments)
        store.update(clip, trim={"mode": "source", "start": start, "end": end})
    else:
        path = vod_dir / "clips" / clip
        backup = vod_dir / "clips" / ".orig" / clip
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        clip_stage.trim_clip(path, path, start, end)
        store.update(clip, trim=None)
    return window(vod_dir, clip, moments, cfg)


def reset_trim(vod_dir: Path, clip: str, moments: dict[int, dict], cfg: dict,
               facecam: dict | None, segments) -> dict:
    vod_dir = Path(vod_dir)
    store = PostStore(vod_dir)
    if is_locked(store.get(clip)):
        raise LockedError("erst Post abbrechen")
    backup = vod_dir / "clips" / ".orig" / clip
    if clip_kind(clip, moments) == "pipeline" and _source(vod_dir).exists():
        o_start, o_end = _orig_window(_moment(clip, moments), cfg)
        _render_source(vod_dir, clip, o_start, o_end, moments, cfg, facecam, segments)
        backup.unlink(missing_ok=True)
    elif backup.exists():
        os.replace(backup, vod_dir / "clips" / clip)
    else:
        raise ValueError("nichts zurückzusetzen")
    store.update(clip, trim=None)
    return window(vod_dir, clip, moments, cfg)
