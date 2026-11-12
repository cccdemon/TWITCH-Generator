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
