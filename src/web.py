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
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import oauth
from . import twitch as twitch_stage
from .config import data_dir, root_path
from .jobs import manager
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


def _vod_groups() -> list[dict]:
    """VODs that have rendered clips: [{vod, count}], newest first."""
    out: list[dict] = []
    for vod_dir in sorted(data_dir().iterdir(), key=lambda p: p.name, reverse=True):
        clips_dir = vod_dir / "clips"
        if not clips_dir.is_dir():
            continue
        n = sum(1 for _ in clips_dir.glob("*.mp4"))
        if n:
            out.append({"vod": vod_dir.name, "count": n})
    return out


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
            "oauth": oauth.all_status(),
            "public_url": oauth.public_url(),
            "clip_groups": _vod_groups(),
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


@app.get("/api/jobs")
def api_jobs(_: str = Depends(current_user)) -> JSONResponse:
    return JSONResponse([
        {"id": j.id, "vod": j.vod, "title": j.title, "status": j.status,
         "created": j.created, "finished": j.finished, "error": j.error}
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


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, _: str = Depends(current_user)) -> JSONResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return JSONResponse({"status": job.status, "log": job.log, "error": job.error})


# --------------------------------------------------------------- clips ---
@app.get("/clips", response_class=HTMLResponse)
def clips_index(request: Request, _: str = Depends(current_user)) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "clips_index.html", {"base": BASE, "groups": _vod_groups()}
    )


@app.get("/clips/{vod_id}", response_class=HTMLResponse)
def clips_vod(request: Request, vod_id: str, _: str = Depends(current_user)) -> HTMLResponse:
    if not _SAFE_NAME.match(vod_id):
        raise HTTPException(404, "no such vod")
    clips = _clips_detail(vod_id)
    if not clips:
        raise HTTPException(404, "no clips for this vod")
    return _TEMPLATES.TemplateResponse(
        request, "clips_vod.html", {"base": BASE, "vod": vod_id, "clips": clips}
    )


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
