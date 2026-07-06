"""FastAPI web interface: login, settings editor, VOD browser, job dashboard.

Auth: per-user username+password (from the USERS env var) backed by a signed
session cookie. Served under an optional URL prefix (ROOT_PATH, e.g. /vod) when
behind the suite reverse proxy — the proxy strips the prefix, so routes stay at
root and only outward links/redirects carry it via `base`.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import clip as clip_stage
from . import oauth
from . import pricing
from . import timing
from . import twitch as twitch_stage
from .config import data_dir, load_config, root_path
from .jobs import manager
from .models import TranscriptSegment

# What to keep when "freeing" a VOD (delete the heavy source, keep the output).
_FREE_KEEP = {"clips", "moments.json", "cost.json", "info.json"}
from .settings_store import (
    SETTING_KEYS,
    apply_settings,
    current_view,
    load_settings,
    save_settings,
)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
BASE = root_path()  # external prefix, e.g. "/vod"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _vod_label(vod_id: str) -> str | None:
    """'Streamer — Stream title' from info.json, or None if not resolved yet."""
    p = data_dir() / vod_id / "info.json"
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    title = (m.get("title") or "").strip()
    streamer = (m.get("streamer") or "").strip()
    if title and streamer:
        return f"{streamer} — {title}"
    return title or streamer or None


def _source_mb(vod_dir: Path) -> float:
    """Size of the raw VOD download (the disk hog), in MB. 0 if already freed."""
    src = vod_dir / f"{vod_dir.name}.mp4"
    if src.exists():
        return round(src.stat().st_size / 1e6, 1)
    return 0.0


def _vod_groups() -> list[dict]:
    """VODs with clips OR detected moments (for the editor), newest first."""
    out: list[dict] = []
    for vod_dir in sorted(data_dir().iterdir(), key=lambda p: p.name, reverse=True):
        if not vod_dir.is_dir():
            continue
        clips_dir = vod_dir / "clips"
        n = sum(1 for _ in clips_dir.glob("*.mp4")) if clips_dir.is_dir() else 0
        has_moments = (vod_dir / "moments.json").exists()
        if n or has_moments:
            cost = pricing.load_cost(vod_dir / "cost.json")
            out.append({
                "vod": vod_dir.name,
                "count": n,
                "label": _vod_label(vod_dir.name),
                "usd": cost["usd"] if cost else None,
                "src_mb": _source_mb(vod_dir),
            })
    return out


def _job_vod_id(vod: str) -> str | None:
    try:
        return twitch_stage.parse_vod_id(vod)
    except ValueError:
        return None


def _job_etas() -> dict[str, float]:
    """job_id -> seconds remaining, for running jobs that have a run.json."""
    out: dict[str, float] = {}
    for j in manager.list():
        if j.status != "running":
            continue
        vid = _job_vod_id(j.vod)
        if not vid:
            continue
        left = timing.remaining_seconds(vid)
        if left is not None:
            out[j.id] = round(left)
    return out


def _job_labels() -> dict[str, str]:
    """job_id -> 'Streamer — Title', once the VOD has been resolved."""
    out: dict[str, str] = {}
    for j in manager.list():
        vid = _job_vod_id(j.vod)
        if not vid:
            continue
        label = _vod_label(vid)
        if label:
            out[j.id] = label
    return out


def _job_costs() -> dict[str, float]:
    """job_id -> predicted Claude cost (USD), for jobs with a run.json estimate."""
    out: dict[str, float] = {}
    for j in manager.list():
        if j.status not in ("running", "queued"):
            continue
        vid = _job_vod_id(j.vod)
        if not vid:
            continue
        run = timing.read_run(vid)
        if run and run.get("est_cost"):
            out[j.id] = round(float(run["est_cost"]), 2)
    return out


def _busy_vod_ids() -> set[str]:
    """VOD ids that have a running/queued job — must not be deleted."""
    ids: set[str] = set()
    for j in manager.list():
        if j.status in ("running", "queued"):
            vid = _job_vod_id(j.vod)
            if vid:
                ids.add(vid)
    return ids


def _delete_vod(vod_id: str, *, keep_clips: bool) -> None:
    """Free the source (keep_clips) or wipe the whole VOD dir. Irreversible."""
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    root = data_dir().resolve()
    vdir = (root / vod_id).resolve()
    if not str(vdir).startswith(str(root) + os.sep) or not vdir.is_dir():
        raise HTTPException(404, "no such vod")
    if vod_id in _busy_vod_ids():
        raise HTTPException(409, "VOD is being processed — cancel the job first")
    if keep_clips:
        for child in vdir.iterdir():
            if child.name in _FREE_KEEP:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    else:
        shutil.rmtree(vdir, ignore_errors=True)


def _moments_map(vod_id: str) -> dict[int, dict]:
    """Map clip-start-second -> moment record (title/score/components)."""
    p = data_dir() / vod_id / "moments.json"
    out: dict[int, dict] = {}
    if p.exists():
        try:
            for m in json.loads(p.read_text(encoding="utf-8")):
                out[int(m["start"])] = m
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            pass
    return out


def _clips_detail(vod_id: str) -> list[dict]:
    """Clips of one VOD joined with their moment metadata."""
    clips_dir = data_dir() / vod_id / "clips"
    if not clips_dir.is_dir():
        return []
    mm = _moments_map(vod_id)
    res: list[dict] = []
    for f in sorted(clips_dir.glob("*.mp4")):
        lead = f.name.split("_", 1)[0]
        m = mm.get(int(lead)) if lead.isdigit() else None
        m = m or {}
        res.append({
            "name": f.name,
            "size_mb": round(f.stat().st_size / 1e6, 1),
            "title": m.get("title") or f.name,
            "category": m.get("category", ""),
            "score": m.get("score"),
            "audio": m.get("audio_score"),
            "text": m.get("text_score"),
            "video": m.get("video_score"),
        })
    return res


def _load_users() -> dict[str, str]:
    """USERS="alice:pw1,bob:pw2" -> {alice: pw1, bob: pw2}."""
    raw = os.environ.get("USERS", "")
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, pw = pair.split(":", 1)
        if name.strip() and pw:
            out[name.strip()] = pw
    return out


app = FastAPI(title="TWITCH-Generator", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    apply_settings()


async def _require_login(request: Request, call_next):
    path = request.url.path  # proxy already stripped BASE; paths are unprefixed
    if path in ("/login", "/health") or request.session.get("user"):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"detail": "auth required"}, status_code=401)
    return RedirectResponse(f"{BASE}/login", status_code=303)


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "auth required")
    return user


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------- auth ---
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    if request.session.get("user"):
        return RedirectResponse(f"{BASE}/", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request, "login.html", {"base": BASE, "error": error}
    )


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    users = _load_users()
    stored = users.get(username.strip())
    if stored and _secrets.compare_digest(password, stored):
        request.session["user"] = username.strip()
        return RedirectResponse(f"{BASE}/", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"base": BASE, "error": "Invalid credentials"},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(f"{BASE}/login", status_code=303)


# ---------------------------------------------------------------- main views ---
def _render(request: Request, vods=None, message: str | None = None) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "base": BASE,
            "user": request.session.get("user"),
            "settings": current_view(),
            "jobs": manager.list(),
            "vods": vods,
            "message": message,
            "users_configured": bool(_load_users()),
            "clip_groups": _vod_groups(),
            "job_etas": _job_etas(),
            "job_labels": _job_labels(),
            "job_costs": _job_costs(),
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str | None = None, _: str = Depends(current_user)) -> HTMLResponse:
    return _render(request, message=msg)


@app.post("/settings", response_class=HTMLResponse)
async def update_settings(request: Request, _: str = Depends(current_user)) -> HTMLResponse:
    form = await request.form()
    stored = load_settings()
    for key, _label, secret in SETTING_KEYS:
        val = (form.get(key) or "").strip()
        # For secrets, blank means "keep existing"; plain fields blank clears.
        if secret and not val:
            continue
        stored[key] = val

    yt_file = form.get("youtube_secret_file")
    if isinstance(yt_file, UploadFile) and yt_file.filename:
        dest = data_dir() / "secrets" / "youtube_client_secret.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(await yt_file.read())
        stored["YOUTUBE_CLIENT_SECRETS"] = str(dest)

    save_settings(stored)
    return _render(request, message="Settings saved.")


@app.post("/vods", response_class=HTMLResponse)
def search_vods(request: Request, login: str = Form(...), _: str = Depends(current_user)) -> HTMLResponse:
    apply_settings()
    try:
        vods = twitch_stage.get_user_vods(login)
        msg = f"{len(vods)} VODs for {login}" if vods else f"No VODs for {login}"
    except Exception as e:  # noqa: BLE001
        return _render(request, message=f"VOD lookup failed: {e}")
    return _render(request, vods=vods, message=msg)


@app.post("/jobs")
async def create_jobs(request: Request, _: str = Depends(current_user)) -> RedirectResponse:
    form = await request.form()
    no_upload = form.get("no_upload") == "on"
    selected = list(form.getlist("vod"))
    direct = (form.get("direct_vod") or "").strip()
    if direct:
        selected.append(direct)
    for vod in selected:
        manager.submit(vod, no_upload=no_upload, title=vod)
    return RedirectResponse(f"{BASE}/", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _: str = Depends(current_user)) -> RedirectResponse:
    manager.cancel(job_id)
    return RedirectResponse(f"{BASE}/", status_code=303)


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, _: str = Depends(current_user)) -> RedirectResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    manager.retry(job_id)
    return RedirectResponse(f"{BASE}/", status_code=303)


@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: str, _: str = Depends(current_user)) -> RedirectResponse:
    manager.delete(job_id)
    return RedirectResponse(f"{BASE}/", status_code=303)


@app.post("/jobs/clear")
def clear_jobs(_: str = Depends(current_user)) -> RedirectResponse:
    n = manager.clear_finished()
    return RedirectResponse(f"{BASE}/?msg=" + quote(f"cleared {n} finished job(s)"), status_code=303)


@app.get("/api/jobs")
def api_jobs(_: str = Depends(current_user)) -> JSONResponse:
    etas = _job_etas()
    labels = _job_labels()
    costs = _job_costs()
    return JSONResponse([
        {"id": j.id, "vod": j.vod, "title": j.title, "status": j.status,
         "created": j.created, "finished": j.finished, "error": j.error,
         "eta": etas.get(j.id), "label": labels.get(j.id), "est_cost": costs.get(j.id)}
        for j in manager.list()
    ])


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, _: str = Depends(current_user)) -> HTMLResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return _TEMPLATES.TemplateResponse(
        request, "job.html", {"base": BASE, "job": job}
    )


@app.get("/api/clips/{vod_id}")
def api_clips(vod_id: str, _: str = Depends(current_user)) -> JSONResponse:
    """Live clip list for one VOD — used to append clips as they finish rendering."""
    busy = any(j.status in ("running", "queued") for j in manager.list())
    return JSONResponse({"busy": busy, "clips": _clips_detail(vod_id)})


@app.get("/api/clip-groups")
def api_clip_groups(_: str = Depends(current_user)) -> JSONResponse:
    """Live VOD list for the clips index."""
    busy = any(j.status in ("running", "queued") for j in manager.list())
    return JSONResponse({"busy": busy, "groups": _vod_groups()})


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, _: str = Depends(current_user)) -> JSONResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return JSONResponse({"status": job.status, "log": job.log, "error": job.error})


# --------------------------------------------------------------- clips ---
@app.get("/clips", response_class=HTMLResponse)
def clips_index(request: Request, msg: str | None = None, _: str = Depends(current_user)) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "clips_index.html", {"base": BASE, "groups": _vod_groups(), "message": msg}
    )


@app.get("/clips/{vod_id}", response_class=HTMLResponse)
def clips_vod(request: Request, vod_id: str, msg: str | None = None, _: str = Depends(current_user)) -> HTMLResponse:
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    vdir = data_dir() / vod_id
    if not vdir.is_dir():
        raise HTTPException(404, "no such vod")
    clips = _clips_detail(vod_id)  # may be empty (auto_render off → build in editor)
    cost = pricing.load_cost(data_dir() / vod_id / "cost.json")
    return _TEMPLATES.TemplateResponse(
        request, "clips_vod.html",
        {
            "base": BASE, "vod": vod_id, "clips": clips, "message": msg,
            "label": _vod_label(vod_id),
            "usd": cost["usd"] if cost else None,
            "src_mb": _source_mb(data_dir() / vod_id),
        },
    )


def _moments_list(vod_id: str) -> list[dict]:
    """Highlight windows [{start,end,title,category}] for the editor's quick-jump."""
    p = data_dir() / vod_id / "moments.json"
    out: list[dict] = []
    if p.exists():
        try:
            for m in json.loads(p.read_text(encoding="utf-8")):
                out.append({
                    "start": round(float(m["start"]), 2),
                    "end": round(float(m["end"]), 2),
                    "title": m.get("title", ""),
                    "category": m.get("category", ""),
                })
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            pass
    return sorted(out, key=lambda x: x["start"])


def _load_facecam(vod_id: str) -> dict | None:
    p = data_dir() / vod_id / "facecam.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d or None
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _file_iter(path: Path, start: int, end: int, chunk: int = 1 << 20):
    """Yield bytes [start, end] of a file in chunks (for HTTP Range responses)."""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@app.get("/source/{vod_id}")
def source_video(vod_id: str, request: Request, _: str = Depends(current_user)):
    """Serve the raw VOD with HTTP Range (206) so the editor can scrub/seek it.

    Starlette's FileResponse doesn't reliably honour Range, and large VODs are
    usually not faststart — without 206 the browser can't start playback until
    the whole file arrives. So we handle Range ourselves.
    """
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    src = (data_dir() / vod_id / f"{vod_id}.mp4").resolve()
    root = (data_dir() / vod_id).resolve()
    if not str(src).startswith(str(root) + os.sep) or not src.is_file():
        raise HTTPException(404, "source video not available (deleted?)")

    size = src.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(src, media_type="video/mp4",
                            headers={"Accept-Ranges": "bytes"})

    # "bytes=START-END", "bytes=START-", or suffix "bytes=-N" (last N bytes —
    # browsers use this to fetch the moov atom of a non-faststart mp4).
    try:
        unit, _, rng = range_header.partition("=")
        if unit.strip() != "bytes":
            raise ValueError
        start_s, _, end_s = rng.partition("-")
        if start_s == "":
            if end_s == "":
                raise ValueError
            length = int(end_s)              # suffix range: final `length` bytes
            if length <= 0:
                raise ValueError
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        raise HTTPException(416, "invalid range")
    end = min(end, size - 1)
    if start < 0 or start > end or start >= size:
        return JSONResponse({"detail": "range not satisfiable"}, status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(_file_iter(src, start, end), status_code=206,
                             headers=headers, media_type="video/mp4")


@app.get("/edit/{vod_id}", response_class=HTMLResponse)
def editor(request: Request, vod_id: str, _: str = Depends(current_user)) -> HTMLResponse:
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    src = data_dir() / vod_id / f"{vod_id}.mp4"
    c = (load_config().get("clip") or {})
    return _TEMPLATES.TemplateResponse(
        request, "edit.html",
        {
            "base": BASE, "vod": vod_id, "label": _vod_label(vod_id),
            "moments": _moments_list(vod_id),
            "facecam": _load_facecam(vod_id),
            "has_source": src.exists(),
            "width": c.get("width", 1080), "height": c.get("height", 1920),
        },
    )


@app.post("/render/{vod_id}")
def render_clip_custom(
    vod_id: str,
    start: float = Form(...), end: float = Form(...),
    layout: str = Form("split"), subtitles: str | None = Form(None),
    title: str = Form(""),
    cam_x: float = Form(0.0), cam_y: float = Form(0.0),
    cam_w: float = Form(0.22), cam_h: float = Form(0.22),
    game_x: float = Form(0.0), game_y: float = Form(0.0),
    game_w: float = Form(1.0), game_h: float = Form(1.0),
    _: str = Depends(current_user),
) -> JSONResponse:
    """Render one clip on demand from the editor's trim + crop selection."""
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    src = data_dir() / vod_id / f"{vod_id}.mp4"
    if not src.exists():
        raise HTTPException(404, "source video not available")
    duration = end - start
    if duration < 0.5:
        raise HTTPException(400, "clip too short")

    cfg = load_config().get("clip") or {}
    segments = None
    if subtitles == "on":
        tp = data_dir() / vod_id / "transcript.json"
        if tp.exists():
            try:
                segments = [TranscriptSegment(**s)
                            for s in json.loads(tp.read_text(encoding="utf-8"))]
            except (json.JSONDecodeError, OSError, TypeError):
                segments = None

    safe = "".join(ch if ch.isalnum() else "_" for ch in (title or "clip"))[:40] or "clip"
    name = f"{int(start)}_edit_{safe}.mp4"
    out_path = data_dir() / vod_id / "clips" / name
    try:
        clip_stage.render_custom(
            src, out_path,
            start=start, duration=duration,
            width=cfg.get("width", 1080), height=cfg.get("height", 1920),
            layout="split" if layout == "split" else "crop",
            facecam={"x": cam_x, "y": cam_y, "w": cam_w, "h": cam_h},
            game={"x": game_x, "y": game_y, "w": game_w, "h": game_h},
            segments=segments, burn_subtitles=(subtitles == "on"), title=title,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"render failed: {e}")
    return JSONResponse({"ok": True, "name": name})


@app.get("/frame/{vod_id}")
def frame_image(vod_id: str, _: str = Depends(current_user)) -> FileResponse:
    """A representative source frame for the facecam picker."""
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    vdir = data_dir() / vod_id
    frames = sorted((vdir / "frames").glob("f_*.jpg"))
    if frames:
        return FileResponse(frames[len(frames) // 2], media_type="image/jpeg")
    preview = vdir / "facecam_preview.jpg"
    if preview.exists():
        return FileResponse(preview, media_type="image/jpeg")
    src = vdir / f"{vod_id}.mp4"
    if src.exists():
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "60", "-i", str(src),
                 "-frames:v", "1", "-q:v", "3", str(preview)],
                check=True, capture_output=True,
            )
        except (subprocess.CalledProcessError, OSError):
            pass
        if preview.exists():
            return FileResponse(preview, media_type="image/jpeg")
    raise HTTPException(404, "no frame available — process this VOD first")


@app.get("/facecam/{vod_id}", response_class=HTMLResponse)
def facecam_picker(request: Request, vod_id: str, _: str = Depends(current_user)) -> HTMLResponse:
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    box = None
    p = data_dir() / vod_id / "facecam.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data:
                box = data
        except (json.JSONDecodeError, OSError):
            pass
    return _TEMPLATES.TemplateResponse(
        request, "facecam.html",
        {"base": BASE, "vod": vod_id, "label": _vod_label(vod_id), "box": box},
    )


@app.post("/facecam/{vod_id}")
def facecam_save(
    vod_id: str,
    x: float = Form(...), y: float = Form(...),
    w: float = Form(...), h: float = Form(...),
    _: str = Depends(current_user),
) -> RedirectResponse:
    """Save a manually-picked facecam box (overrides auto-detect on next render)."""
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")

    def _clamp01(v: float, lo: float = 0.0) -> float:
        return max(lo, min(1.0, v))

    bx, by = _clamp01(x), _clamp01(y)
    bw, bh = _clamp01(w, 0.02), _clamp01(h, 0.02)
    bw = min(bw, 1.0 - bx)
    bh = min(bh, 1.0 - by)
    box = {"x": round(bx, 4), "y": round(by, 4),
           "w": round(bw, 4), "h": round(bh, 4), "manual": True}
    vdir = data_dir() / vod_id
    if not vdir.is_dir():
        raise HTTPException(404, "no such vod")
    try:
        (vdir / "facecam.json").write_text(json.dumps(box), encoding="utf-8")
    except OSError:
        raise HTTPException(500, "could not save")
    return RedirectResponse(
        f"{BASE}/clips/{vod_id}?msg=" + quote("Facecam box saved — retry the job to re-render"),
        status_code=303,
    )


@app.post("/vods/{vod_id}/free")
def free_vod(vod_id: str, _: str = Depends(current_user)) -> RedirectResponse:
    """Delete the raw source download + intermediates, keep the rendered clips."""
    _delete_vod(vod_id, keep_clips=True)
    return RedirectResponse(f"{BASE}/clips?msg=" + quote(f"VOD {vod_id}: source freed"), status_code=303)


@app.post("/vods/{vod_id}/delete")
def delete_vod(vod_id: str, _: str = Depends(current_user)) -> RedirectResponse:
    """Wipe the whole VOD directory, including rendered clips. Irreversible."""
    _delete_vod(vod_id, keep_clips=False)
    return RedirectResponse(f"{BASE}/clips?msg=" + quote(f"VOD {vod_id}: deleted"), status_code=303)


@app.get("/clips/{vod_id}/{filename}")
def clip_file(vod_id: str, filename: str, _: str = Depends(current_user)) -> FileResponse:
    """Serve a clip inline (Range-capable) so it previews and downloads."""
    if not (_SAFE_NAME.match(vod_id) and _SAFE_NAME.match(filename) and filename.endswith(".mp4")):
        raise HTTPException(400, "bad name")
    clips_root = (data_dir() / vod_id / "clips").resolve()
    path = (clips_root / filename).resolve()
    if not str(path).startswith(str(clips_root) + os.sep) or not path.is_file():
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="video/mp4")


# ------------------------------------------------------ OAuth connect ---
@app.get("/oauth/{platform}/start")
def oauth_start(platform: str, request: Request, _: str = Depends(current_user)) -> RedirectResponse:
    if platform not in oauth.PLATFORMS:
        raise HTTPException(404, "unknown platform")
    if not oauth.configured(platform):
        return RedirectResponse(
            f"{BASE}/?msg=" + quote(f"{platform}: set PUBLIC_URL + client id/secret in Settings first"),
            status_code=303,
        )
    state = _secrets.token_urlsafe(24)
    request.session[f"oauth_state_{platform}"] = state
    return RedirectResponse(oauth.build_auth_url(platform, state), status_code=303)


@app.get("/oauth/{platform}/callback")
def oauth_callback(
    request: Request,
    platform: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    _: str = Depends(current_user),
) -> RedirectResponse:
    expected = request.session.pop(f"oauth_state_{platform}", None)
    if error:
        msg = f"{platform} connect denied: {error}"
    elif not code or not state or state != expected:
        msg = f"{platform} connect failed: state mismatch"
    else:
        try:
            oauth.exchange_code(platform, code)
            msg = f"{platform} connected"
        except Exception as e:  # noqa: BLE001
            msg = f"{platform} connect failed: {e}"
    return RedirectResponse(f"{BASE}/?msg=" + quote(msg), status_code=303)


@app.post("/oauth/{platform}/disconnect")
def oauth_disconnect(platform: str, _: str = Depends(current_user)) -> RedirectResponse:
    oauth.disconnect(platform)
    return RedirectResponse(f"{BASE}/?msg=" + quote(f"{platform} disconnected"), status_code=303)


# Middleware order matters: Starlette runs the LAST-added middleware first
# (outermost). SessionMiddleware must run before _require_login so request.session
# is populated when the auth gate checks it — hence it is added last.
app.add_middleware(BaseHTTPMiddleware, dispatch=_require_login)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or _secrets.token_hex(32),
    same_site="lax",
    https_only=False,  # TLS terminates at the proxy; cookie rides HTTPS proxy<->browser.
)
