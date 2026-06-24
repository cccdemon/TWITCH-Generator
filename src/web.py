"""FastAPI web interface: login, settings editor, VOD browser, job dashboard.

Auth: per-user username+password (from the USERS env var) backed by a signed
session cookie. Served under an optional URL prefix (ROOT_PATH, e.g. /vod) when
behind the suite reverse proxy — the proxy strips the prefix, so routes stay at
root and only outward links/redirects carry it via `base`.
"""
from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

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
        "login.html", {"request": request, "base": BASE, "error": error}
    )


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    users = _load_users()
    stored = users.get(username.strip())
    if stored and _secrets.compare_digest(password, stored):
        request.session["user"] = username.strip()
        return RedirectResponse(f"{BASE}/", status_code=303)
    return _TEMPLATES.TemplateResponse(
        "login.html",
        {"request": request, "base": BASE, "error": "Invalid credentials"},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(f"{BASE}/login", status_code=303)


# ---------------------------------------------------------------- main views ---
def _render(request: Request, vods=None, message: str | None = None) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "base": BASE,
            "user": request.session.get("user"),
            "settings": current_view(),
            "jobs": manager.list(),
            "vods": vods,
            "message": message,
            "users_configured": bool(_load_users()),
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: str = Depends(current_user)) -> HTMLResponse:
    return _render(request)


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
        "job.html", {"request": request, "base": BASE, "job": job}
    )


@app.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, _: str = Depends(current_user)) -> JSONResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return JSONResponse({"status": job.status, "log": job.log, "error": job.error})


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
