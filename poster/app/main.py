"""clip-poster: posts rendered clips to TikTok / Instagram through a USB-attached phone.

Headless API for the generator web UI (which owns the editing GUI). Jobs are
queued with a run time; one worker thread drives the phone strictly one post
at a time. Auth: shared bearer token (POSTER_TOKEN). No platform passwords are
stored anywhere: the apps on the phone stay logged in, we only tap.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import threading
import time
import traceback
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .flows import FLOWS
from .phone import Phone, PhoneError
from .store import Store

DATA = Path(os.environ.get("DATA_DIR", "/data"))
TOKEN = os.environ.get("POSTER_TOKEN", "")
SERIAL = os.environ.get("ADB_SERIAL", "")
ACCOUNTS = {
    "tiktok": os.environ.get("TIKTOK_ACCOUNT", "justcallme_deimos"),
    "instagram": os.environ.get("INSTAGRAM_ACCOUNT", "justcallmedeimos"),
}
KEEP_MEDIA_DAYS = float(os.environ.get("KEEP_MEDIA_DAYS", "7"))
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

MEDIA = DATA / "media"
SHOTS = DATA / "shots"
store = Store(DATA / "poster.db")
app = FastAPI(title="clip-poster")

_state = {"current": None, "phase": "", "last_error": ""}
_phone_lock = threading.Lock()   # worker and status probes never talk to adb at the same time


def auth(authorization: str = Header(default="")) -> None:
    if not TOKEN:
        raise HTTPException(503, "POSTER_TOKEN not configured")
    given = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, TOKEN):
        raise HTTPException(401, "bad token")


def _media_path(pid: int) -> Path:
    return MEDIA / f"{pid}.mp4"


def _shot_path(pid: int, platform: str) -> Path:
    return SHOTS / f"{pid}_{platform}.png"


def _public(post: dict) -> dict:
    out = dict(post)
    out["shots"] = [p for p in post["platforms"] if _shot_path(post["id"], p).exists()]
    out["media"] = _media_path(post["id"]).exists()
    return out


# ------------------------------------------------------------------ worker
def _run_post(post: dict) -> None:
    pid = post["id"]
    phone = Phone(SERIAL, log=lambda s: store.append_log(pid, s))
    results = dict(post["results"] or {})
    store.update(pid, status="running")
    store.append_log(pid, f"Start ({'Probelauf' if post['dry_run'] else 'echt'}): "
                          f"{', '.join(post['platforms'])}")
    for platform in post["platforms"]:
        if (results.get(platform) or {}).get("ok"):
            continue  # retry: skip what already worked
        flow_cls = FLOWS.get(platform)
        if not flow_cls:
            results[platform] = {"ok": False, "msg": "unbekannte Plattform", "at": time.time()}
            continue
        _state["phase"] = platform
        store.append_log(pid, f"— {flow_cls.name} —")
        flow = flow_cls(phone, ACCOUNTS[platform], post["dry_run"], lambda s: store.append_log(pid, s),
                        shot_path=_shot_path(pid, platform))
        try:
            msg = flow.run(str(_media_path(pid)), post["clip_name"], post["caption"], post["options"] or {})
            results[platform] = {"ok": True, "msg": msg, "at": time.time()}
            store.append_log(pid, f"{flow_cls.name}: {msg}")
        except PhoneError as e:
            results[platform] = {"ok": False, "msg": str(e), "at": time.time()}
            store.append_log(pid, f"FEHLER {e}")
        except Exception as e:  # noqa: BLE001
            results[platform] = {"ok": False, "msg": f"interner Fehler: {e}", "at": time.time()}
            store.append_log(pid, "FEHLER " + traceback.format_exc()[-800:])
        store.update(pid, results=results)
        time.sleep(3)
    oks = [bool((results.get(p) or {}).get("ok")) for p in post["platforms"]]
    status = "done" if all(oks) else ("partial" if any(oks) else "failed")
    store.update(pid, status=status, results=results, finished=time.time())
    store.append_log(pid, f"Ende: {status}")


def _housekeeping() -> None:
    cutoff = time.time() - KEEP_MEDIA_DAYS * 86400
    for pid in store.old_finished(cutoff):
        _media_path(pid).unlink(missing_ok=True)


def _worker() -> None:
    last_hk = 0.0
    while True:
        try:
            post = store.next_due(time.time())
            if not post:
                if time.time() - last_hk > 3600:
                    _housekeeping()
                    last_hk = time.time()
                time.sleep(5)
                continue
            if not _media_path(post["id"]).exists():
                store.update(post["id"], status="failed", finished=time.time())
                store.append_log(post["id"], "FEHLER Videodatei fehlt")
                continue
            with _phone_lock:
                _state["current"] = post["id"]
                _run_post(post)
        except Exception as e:  # noqa: BLE001
            _state["last_error"] = str(e)
            time.sleep(10)
        finally:
            _state["current"] = None
            _state["phase"] = ""


@app.on_event("startup")
def _start() -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_worker, name="poster-worker", daemon=True).start()


# --------------------------------------------------------------------- API
@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/status", dependencies=[Depends(auth)])
def status() -> JSONResponse:
    info = {"serial": SERIAL, "accounts": ACCOUNTS, "busy": _state["current"] is not None,
            "current": _state["current"], "phase": _state["phase"], "online": None, "battery": None}
    if _phone_lock.acquire(timeout=0.2):
        try:
            ph = Phone(SERIAL)
            info["online"] = ph.online()
            if info["online"]:
                m = re.search(r"level: (\d+)", ph.sh("dumpsys", "battery", timeout=10))
                info["battery"] = int(m.group(1)) if m else None
        except PhoneError:
            info["online"] = False
        finally:
            _phone_lock.release()
    queued = [p for p in store.list() if p["status"] == "queued"]
    info["queued"] = len(queued)
    info["next_run"] = min((p["run_at"] for p in queued), default=None)
    return JSONResponse(info)


@app.post("/api/posts", dependencies=[Depends(auth)])
async def create_post(video: UploadFile = File(...), meta: str = Form(...)) -> JSONResponse:
    try:
        m = json.loads(meta)
    except json.JSONDecodeError:
        raise HTTPException(400, "meta must be JSON")
    platforms = [p for p in m.get("platforms", []) if p in FLOWS]
    if not platforms:
        raise HTTPException(400, "no platform selected")
    caption = str(m.get("caption", "")).strip()
    if not caption:
        raise HTTPException(400, "caption empty")
    if "instagram" in platforms and len(caption) > 2200:
        raise HTTPException(400, "Instagram: Text länger als 2200 Zeichen")
    if "instagram" in platforms and caption.count("#") > 30:
        raise HTTPException(400, "Instagram: mehr als 30 Hashtags")
    if "tiktok" in platforms and len(caption) > 4000:
        raise HTTPException(400, "TikTok: Text länger als 4000 Zeichen")
    name = str(m.get("clip_name", "clip.mp4"))
    if not _SAFE.match(name) or not name.endswith(".mp4"):
        name = "clip.mp4"
    run_at = float(m.get("run_at") or time.time())
    pid = store.add(run_at=run_at, clip_name=name, title=str(m.get("title", ""))[:200],
                    source=str(m.get("source", ""))[:200], caption=caption, platforms=platforms,
                    options={"threads": bool(m.get("threads", True))}, dry_run=bool(m.get("dry_run")))
    MEDIA.mkdir(parents=True, exist_ok=True)
    tmp = _media_path(pid).with_suffix(".part")
    with tmp.open("wb") as fh:
        shutil.copyfileobj(video.file, fh, length=1 << 20)
    tmp.replace(_media_path(pid))
    return JSONResponse({"id": pid})


@app.get("/api/posts", dependencies=[Depends(auth)])
def list_posts() -> JSONResponse:
    return JSONResponse({"posts": [_public(p) for p in store.list()], "current": _state["current"]})


@app.get("/api/posts/{pid}", dependencies=[Depends(auth)])
def get_post(pid: int) -> JSONResponse:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "no such post")
    return JSONResponse(_public(p))


@app.patch("/api/posts/{pid}", dependencies=[Depends(auth)])
def edit_post(pid: int, body: dict) -> JSONResponse:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "no such post")
    if p["status"] != "queued":
        raise HTTPException(409, "nur geplante Posts sind änderbar")
    upd = {}
    if "caption" in body and str(body["caption"]).strip():
        upd["caption"] = str(body["caption"]).strip()
    if "run_at" in body:
        upd["run_at"] = float(body["run_at"] or time.time())
    store.update(pid, **upd)
    return JSONResponse(_public(store.get(pid)))


@app.post("/api/posts/{pid}/cancel", dependencies=[Depends(auth)])
def cancel_post(pid: int) -> JSONResponse:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "no such post")
    if p["status"] != "queued":
        raise HTTPException(409, "läuft bereits oder ist fertig")
    store.update(pid, status="cancelled", finished=time.time())
    return JSONResponse({"ok": True})


@app.post("/api/posts/{pid}/retry", dependencies=[Depends(auth)])
def retry_post(pid: int) -> JSONResponse:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "no such post")
    if p["status"] not in ("failed", "partial", "cancelled"):
        raise HTTPException(409, "nichts zu wiederholen")
    if not _media_path(pid).exists():
        raise HTTPException(410, "Videodatei schon aufgeräumt – bitte neu senden")
    store.update(pid, status="queued", run_at=time.time(), finished=None)
    store.append_log(pid, "erneut eingereiht")
    return JSONResponse({"ok": True})


@app.delete("/api/posts/{pid}", dependencies=[Depends(auth)])
def delete_post(pid: int) -> JSONResponse:
    p = store.get(pid)
    if not p:
        raise HTTPException(404, "no such post")
    if p["status"] == "running":
        raise HTTPException(409, "läuft gerade")
    _media_path(pid).unlink(missing_ok=True)
    for pl in p["platforms"]:
        _shot_path(pid, pl).unlink(missing_ok=True)
    store.delete(pid)
    return JSONResponse({"ok": True})


@app.get("/api/posts/{pid}/shot/{platform}", dependencies=[Depends(auth)])
def shot(pid: int, platform: str) -> FileResponse:
    if platform not in FLOWS:
        raise HTTPException(404, "no such platform")
    path = _shot_path(pid, platform)
    if not path.exists():
        raise HTTPException(404, "no screenshot")
    return FileResponse(path, media_type="image/png")
