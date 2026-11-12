"""Routes for posting clips through the phone service (clip-poster).

Mounted by src/web.py. All paths are under /api, so the login middleware answers
401 instead of redirecting. The browser never talks to the phone service itself:
every call goes through here and the POSTER_TOKEN stays server-side.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import poster_client, posting
from .config import data_dir, load_config
from .models import TranscriptSegment

router = APIRouter()

_SHOT_PLATFORMS = {"tiktok", "instagram"}
# One lock per clip shared by render, reset and send: a render must never swap the
# file while it is being uploaded to the phone, and two renders must not share a .part.
_clip_locks: dict[tuple[str, str], threading.Lock] = {}
_clip_locks_guard = threading.Lock()


class _ClipBusy:
    def __init__(self, vod: str, clip: str):
        with _clip_locks_guard:
            self.lock = _clip_locks.setdefault((vod, clip), threading.Lock())

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(409, "Clip wird gerade gerendert oder gesendet")
        return self

    def __exit__(self, *exc):
        self.lock.release()


def _file_sig(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


# ------------------------------------------------------------------ helpers
def _user(request: Request) -> None:
    if not request.session.get("user"):
        raise HTTPException(401, "auth required")


def _vod_dir(vod: str) -> Path:
    from .web import _SAFE_NAME

    if not _SAFE_NAME.match(vod):
        raise HTTPException(404, "no such vod")
    root = data_dir().resolve()
    d = (root / vod).resolve()
    if d.parent != root or not d.is_dir():
        raise HTTPException(404, "no such vod")
    return d


def _clip_path(vod: str, clip: str) -> Path:
    from .web import _SAFE_NAME

    vdir = _vod_dir(vod)
    if not (_SAFE_NAME.match(clip) and clip.endswith(".mp4")):
        raise HTTPException(404, "no such clip")
    clips = (vdir / "clips").resolve()
    p = (clips / clip).resolve()
    if p.parent != clips:
        raise HTTPException(404, "no such clip")
    return p


def _moments(vod: str) -> dict[int, dict]:
    from .web import _moments_map

    return _moments_map(vod)


def _facecam(vod: str) -> dict | None:
    from .web import _load_facecam

    return _load_facecam(vod) or (load_config().get("clip") or {}).get("facecam")


def _segments(vdir: Path) -> list[TranscriptSegment]:
    try:
        return [TranscriptSegment(**s) for s in
                json.loads((vdir / "transcript.json").read_text(encoding="utf-8"))]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _signal(vdir: Path, name: str) -> list[list[float]]:
    try:
        d = json.loads((vdir / name).read_text(encoding="utf-8"))
        return [[float(t), float(s)] for t, s in (d.get("data") if isinstance(d, dict) else d) or []]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def _clip_meta(clip: str, moments: dict[int, dict]) -> tuple[str, str]:
    """(title, category) for the caption."""
    if posting.clip_kind(clip, moments) == "pipeline":
        m = moments[int(clip.split("_", 1)[0])]
        return m.get("title") or clip, m.get("category", "")
    stem = clip[:-4]
    parts = stem.split("_", 2)
    title = parts[2] if len(parts) == 3 and parts[0].isdigit() else stem
    return title.replace("-", " ").replace("_", " ").strip() or stem, ""


def _vod_offset(vdir: Path, clip: str, moments: dict[int, dict], cfg: dict) -> float | None:
    """VOD second at which the clip file starts, if known (untrimmed pipeline clip)."""
    if posting.clip_kind(clip, moments) != "pipeline":
        return None
    if (vdir / "clips" / ".orig" / clip).exists():
        return None
    trim = posting.PostStore(vdir).get(clip)["trim"]
    if trim and trim.get("mode") == "source":
        return float(trim["start"])
    return posting._orig_window(moments[int(clip.split("_", 1)[0])], cfg)[0]


def _excerpt(vdir: Path, start: float, end: float) -> str:
    return " ".join(s.text.strip() for s in _segments(vdir) if s.end > start and s.start < end)[:1500]


def _fixed_tags() -> list[str]:
    return posting.parse_fixed_tags(os.environ.get("POST_HASHTAGS", ""))


def _suggested_caption(clip: str, moments: dict[int, dict], ai_tags: list[str]) -> str:
    title, _ = _clip_meta(clip, moments)
    return posting.build_caption(
        os.environ.get("POST_TEMPLATE") or posting.DEFAULT_TEMPLATE, title,
        os.environ.get("POST_TWITCH_URL", ""), posting.merge_hashtags(_fixed_tags(), ai_tags))


def _ai_tags(vdir: Path, clip: str, moments: dict[int, dict], cfg: dict) -> list[str]:
    title, category = _clip_meta(clip, moments)
    off = _vod_offset(vdir, clip, moments, cfg)
    excerpt = ""
    if off is not None:
        m = moments[int(clip.split("_", 1)[0])]
        excerpt = _excerpt(vdir, off, float(m["end"]) + 1.0)
    return posting.suggest_hashtags(title, category, excerpt, _fixed_tags(), cfg)


def _merge_remote(rec: dict, remote: dict) -> dict:
    out = dict(rec)
    for k in ("status", "results", "shots", "run_at", "caption", "finished"):
        if k in remote:
            out[k] = remote[k]
    return out


def _refresh(store: posting.PostStore, clip: str, only_active: bool = True,
             ids: set[int] | None = None) -> dict:
    """Pull poster status for the clip's posts and merge by id (never rewrite the list)."""
    updates: dict[int, dict] = {}
    for rec in store.get(clip)["posts"]:
        if ids is not None and rec.get("id") not in ids:
            continue
        if only_active and ids is None and rec.get("status") not in posting.ACTIVE:
            continue
        try:
            updates[rec["id"]] = _merge_remote({}, poster_client.get(rec["id"]))
        except (poster_client.PosterError, KeyError):
            pass
    return store.merge_posts(clip, updates) if updates else store.get(clip)


def _panel(vod: str, clip: str) -> dict:
    vdir = _vod_dir(vod)
    if not _clip_path(vod, clip).is_file():
        raise HTTPException(404, "no such clip")
    cfg = load_config()
    moments = _moments(vod)
    store = posting.PostStore(vdir)
    entry = _refresh(store, clip) if poster_client.configured() else store.get(clip)
    # AI tags are NOT fetched here: under Ollama that can take minutes (model swap,
    # running job). The page asks POST .../hashtags after rendering the panel.
    caption = entry["caption"] or _suggested_caption(clip, moments, entry["ai_tags"] or [])
    sig = _file_sig(_clip_path(vod, clip))
    posts = [{**p, "stale": bool(p.get("file")) and p.get("file") != sig} for p in entry["posts"]]
    return {
        "window": posting.window(vdir, clip, moments, cfg),
        "caption": caption,
        "ai_tags": entry["ai_tags"],
        "fixed_tags": _fixed_tags(),
        "check": posting.check_caption(caption),
        "posts": posts,
        "locked": posting.is_locked(entry),
        "configured": poster_client.configured(),
    }


def _poster_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except poster_client.PosterError as e:
        raise HTTPException(502, str(e)) from e


# ------------------------------------------------------------------- routes
@router.get("/api/poster/status")
def poster_status(request: Request) -> JSONResponse:
    _user(request)
    if not poster_client.configured():
        return JSONResponse({"configured": False, "online": False})
    try:
        return JSONResponse({"configured": True, **poster_client.status()})
    except poster_client.PosterError as e:
        return JSONResponse({"configured": True, "online": False, "error": str(e)})


@router.get("/api/post/shot/{pid}/{platform}")
def post_shot(request: Request, pid: int, platform: str) -> Response:
    _user(request)
    if platform not in _SHOT_PLATFORMS:
        raise HTTPException(404, "no such platform")
    try:
        return Response(poster_client.shot(pid, platform), media_type="image/png",
                        headers={"Cache-Control": "max-age=3600"})
    except poster_client.PosterError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/api/post/{vod}/{clip}")
def post_panel(request: Request, vod: str, clip: str) -> JSONResponse:
    _user(request)
    return JSONResponse(_panel(vod, clip))


@router.get("/api/post/{vod}/{clip}/signals")
def post_signals(request: Request, vod: str, clip: str) -> JSONResponse:
    _user(request)
    vdir = _vod_dir(vod)
    _clip_path(vod, clip)
    cfg = load_config()
    moments = _moments(vod)
    w = posting.window(vdir, clip, moments, cfg)
    if w["mode"] == "source":
        off, lo, hi = 0.0, w["min"], w["max"]
    else:
        off = _vod_offset(vdir, clip, moments, cfg)
        if off is None:
            return JSONResponse({"audio": [], "chat": [], "cuts": []})
        lo, hi = off, off + w["max"]

    def rel(sig):
        return [[round(t - off, 2), s] for t, s in sig if lo <= t <= hi]

    cuts = sorted({round(x - off, 2) for s in _segments(vdir) for x in (s.start, s.end) if lo <= x <= hi})
    return JSONResponse({"audio": rel(_signal(vdir, "audio.json")),
                         "chat": rel(_signal(vdir, "chat.json")), "cuts": cuts})


@router.post("/api/post/{vod}/{clip}/hashtags")
def post_hashtags(request: Request, vod: str, clip: str) -> JSONResponse:
    _user(request)
    vdir = _vod_dir(vod)
    _clip_path(vod, clip)
    moments = _moments(vod)
    tags = _ai_tags(vdir, clip, moments, load_config())
    posting.PostStore(vdir).update(clip, ai_tags=tags, caption=None)
    caption = _suggested_caption(clip, moments, tags)
    return JSONResponse({"ai_tags": tags, "caption": caption, "check": posting.check_caption(caption)})


@router.put("/api/post/{vod}/{clip}/draft")
def post_draft(request: Request, vod: str, clip: str, body: dict = Body(...)) -> JSONResponse:
    _user(request)
    vdir = _vod_dir(vod)
    _clip_path(vod, clip)
    caption = str(body.get("caption") or "").strip()
    posting.PostStore(vdir).update(clip, caption=caption or None)
    return JSONResponse({"check": posting.check_caption(caption)})


def _trim_call(fn, vod: str, clip: str, *args):
    vdir = _vod_dir(vod)
    if not _clip_path(vod, clip).is_file():
        raise HTTPException(404, "no such clip")
    from .web import _busy_vod_ids

    if vod in _busy_vod_ids():
        raise HTTPException(409, "VOD wird gerade verarbeitet – später schneiden")
    try:
        with _ClipBusy(vod, clip):
            return fn(vdir, clip, *args, _moments(vod), load_config(), _facecam(vod), _segments(vdir) or None)
    except HTTPException:
        raise
    except posting.LockedError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001 — ffmpeg failure
        raise HTTPException(500, f"Rendern fehlgeschlagen: {e}") from e


@router.post("/api/post/{vod}/{clip}/trim")
def post_trim(request: Request, vod: str, clip: str, body: dict = Body(...)) -> JSONResponse:
    _user(request)
    try:
        start, end = float(body["start"]), float(body["end"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "start/end fehlen")
    return JSONResponse({"window": _trim_call(posting.apply_trim, vod, clip, start, end)})


@router.post("/api/post/{vod}/{clip}/reset")
def post_reset(request: Request, vod: str, clip: str) -> JSONResponse:
    _user(request)
    return JSONResponse({"window": _trim_call(posting.reset_trim, vod, clip)})


@router.post("/api/post/{vod}/{clip}/send")
def post_send(request: Request, vod: str, clip: str, body: dict = Body(...)) -> JSONResponse:
    _user(request)
    vdir = _vod_dir(vod)
    path = _clip_path(vod, clip)
    caption = str(body.get("caption") or "").strip()
    platforms = [p for p in body.get("platforms") or [] if p in posting.LIMITS]
    if not platforms:
        raise HTTPException(400, "keine Plattform gewählt")
    if not caption:
        raise HTTPException(400, "Text ist leer")
    check = posting.check_caption(caption)
    errors = [check["errors"][p] for p in platforms if not check["ok"][p]]
    if errors:
        raise HTTPException(400, "; ".join(errors))
    if not path.is_file():
        raise HTTPException(404, "Clip-Datei fehlt")
    if not poster_client.configured():
        raise HTTPException(400, "Handy-Dienst nicht eingerichtet (Settings)")

    with _ClipBusy(vod, clip):
        store = posting.PostStore(vdir)
        if posting.is_locked(store.get(clip)):
            raise HTTPException(409, "Clip ist schon geplant – erst abbrechen")
        run_at = float(body.get("run_at") or 0) or time.time()
        title, _ = _clip_meta(clip, _moments(vod))
        meta = {"platforms": platforms, "caption": caption, "clip_name": clip, "title": title,
                "source": f"{vod}/{clip}", "run_at": run_at,
                "threads": bool(body.get("threads", True)), "dry_run": bool(body.get("dry_run"))}
        sig = _file_sig(path)
        pid = _poster_call(poster_client.create, path, meta)
        store.add_post(clip, {"id": pid, "platforms": platforms, "dry_run": meta["dry_run"],
                              "threads": meta["threads"], "run_at": run_at, "created": time.time(),
                              "status": "queued", "caption": caption, "file": sig})
        store.update(clip, caption=caption)
    return JSONResponse(_panel(vod, clip))


@router.patch("/api/post/{vod}/{clip}/posts/{pid}")
def post_edit(request: Request, vod: str, clip: str, pid: int, body: dict = Body(...)) -> JSONResponse:
    _user(request)
    vdir = _vod_dir(vod)
    _clip_path(vod, clip)
    fields = {k: body[k] for k in ("caption", "run_at") if k in body}
    if "caption" in fields:
        check = posting.check_caption(str(fields["caption"]))
        store_entry = posting.PostStore(vdir).get(clip)
        rec = next((p for p in store_entry["posts"] if p.get("id") == pid), None)
        errs = [check["errors"][p] for p in (rec or {}).get("platforms", []) if not check["ok"].get(p, True)]
        if errs:
            raise HTTPException(400, "; ".join(errs))
    _poster_call(poster_client.edit, pid, **fields)
    _refresh(posting.PostStore(vdir), clip, ids={pid})
    return JSONResponse(_panel(vod, clip))


def _post_action(request: Request, vod: str, clip: str, pid: int, fn) -> JSONResponse:
    _user(request)
    vdir = _vod_dir(vod)
    path = _clip_path(vod, clip)
    store = posting.PostStore(vdir)
    if fn is poster_client.retry:
        rec = next((p for p in store.get(clip)["posts"] if p.get("id") == pid), None)
        # The poster retries with ITS copy of the video; after a re-cut that is no
        # longer what the page shows.
        if rec and rec.get("file") and path.is_file() and rec["file"] != _file_sig(path):
            raise HTTPException(409, "Clip wurde seitdem geändert – bitte neu senden")
    _poster_call(fn, pid)
    _refresh(store, clip, ids={pid})
    return JSONResponse(_panel(vod, clip))


@router.post("/api/post/{vod}/{clip}/posts/{pid}/cancel")
def post_cancel(request: Request, vod: str, clip: str, pid: int) -> JSONResponse:
    return _post_action(request, vod, clip, pid, poster_client.cancel)


@router.post("/api/post/{vod}/{clip}/posts/{pid}/retry")
def post_retry(request: Request, vod: str, clip: str, pid: int) -> JSONResponse:
    return _post_action(request, vod, clip, pid, poster_client.retry)
