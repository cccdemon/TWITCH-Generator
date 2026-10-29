"""Upload stage: push rendered clips to YouTube Shorts / TikTok / Instagram Reels.

Tokens come from the in-app OAuth Connect flows (see src/oauth.py) — connect each
platform once in the web UI. Each upload refreshes the token before use. Public
publishing still depends on each platform's app review; uploads no-op with a clear
message when a platform isn't connected.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from . import oauth
from .config import env
from .models import Clip, UploadResult


# ---------------------------------------------------------------- YouTube ----
def _youtube_upload(clip: Clip, privacy: str) -> UploadResult:
    token = oauth.token_for("youtube")
    if not token:
        return UploadResult("youtube", False, error="not connected (Settings → Connect YouTube)")
    rec = oauth.record("youtube")

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=token,
        refresh_token=rec.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": clip.moment.title[:100],
            "description": f"{clip.moment.reason}\n\n#shorts",
            "tags": [clip.moment.category, "shorts", "twitch"],
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(clip.path, mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    vid = resp["id"]
    return UploadResult("youtube", True, url=f"https://youtu.be/{vid}")


# ----------------------------------------------------------------- TikTok ----
_TT = "https://open.tiktokapis.com/v2/post/publish"
_TT_MAX_CHUNK = 64 * 1024 * 1024
_TT_CHUNK = 32 * 1024 * 1024
_TT_TERMINAL = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"}


def _tiktok_chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules:
    files up to 64 MB go as one chunk; larger files split into 32 MB chunks,
    the last one absorbing the remainder (count = size // chunk, >= 1)."""
    if size <= _TT_MAX_CHUNK:
        return size, 1
    return _TT_CHUNK, size // _TT_CHUNK


def tiktok_status(publish_id: str, token: str) -> dict:
    r = httpx.post(f"{_TT}/status/fetch/", json={"publish_id": publish_id},
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json; charset=UTF-8"},
                   timeout=30)
    r.raise_for_status()
    return r.json().get("data", {})


def tiktok_send(path: str | Path, *, title: str = "", mode: str = "draft",
                privacy: str = "SELF_ONLY", wait_seconds: float = 90) -> UploadResult:
    """Upload one video to TikTok.

    mode="draft"  -> creator inbox (scope video.upload). The creator finishes the
                     post in the TikTok app. Works for unaudited apps and public
                     accounts; the API cannot set a caption in this mode.
    mode="direct" -> Direct Post (scope video.publish). Unaudited apps are limited
                     to privacy SELF_ONLY on a private account until TikTok's audit.
    """
    token = oauth.token_for("tiktok")
    if not token:
        return UploadResult("tiktok", False, error="not connected (Settings → Connect TikTok)")
    path = Path(path)
    try:
        size = path.stat().st_size
        chunk, count = _tiktok_chunks(size)
        body: dict = {"source_info": {
            "source": "FILE_UPLOAD", "video_size": size,
            "chunk_size": chunk, "total_chunk_count": count,
        }}
        if mode == "direct":
            endpoint = f"{_TT}/video/init/"
            body["post_info"] = {
                "title": (title or "")[:150], "privacy_level": privacy,
                "disable_comment": False, "disable_duet": False, "disable_stitch": False,
            }
        else:
            endpoint = f"{_TT}/inbox/video/init/"
        init = httpx.post(endpoint, json=body, timeout=60,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json; charset=UTF-8"})
        init.raise_for_status()
        j = init.json()
        err = j.get("error") or {}
        if err.get("code", "ok") != "ok":
            return UploadResult("tiktok", False,
                                error=f"init: {err.get('code')} {err.get('message', '')}".strip())
        data = j["data"]
        upload_url, publish_id = data["upload_url"], data["publish_id"]

        with open(path, "rb") as fh, httpx.Client(timeout=600) as client:
            first = 0
            for i in range(count):
                n = chunk if i < count - 1 else size - first   # last chunk takes the rest
                blob = fh.read(n)
                last = first + len(blob) - 1
                put = client.put(upload_url, content=blob, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(blob)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                })
                if put.status_code not in (200, 201, 206):
                    return UploadResult(
                        "tiktok", False,
                        error=f"upload chunk {i + 1}/{count}: HTTP {put.status_code} {put.text[:200]}")
                first = last + 1

        # Poll until TikTok has taken the file over (inbox) or posted it (direct).
        status, reason, t0 = "", "", time.time()
        while time.time() - t0 < wait_seconds:
            st = tiktok_status(publish_id, token)
            status, reason = st.get("status", ""), st.get("fail_reason", "")
            if status in _TT_TERMINAL:
                break
            time.sleep(3)
        if status == "FAILED":
            return UploadResult("tiktok", False, error=f"{reason or 'failed'} ({publish_id})")
        where = {"SEND_TO_USER_INBOX": "in TikTok inbox — finish the post in the app",
                 "PUBLISH_COMPLETE": "posted"}.get(status, f"processing ({status or 'pending'})")
        return UploadResult("tiktok", True, url=f"{where} · {publish_id}")
    except httpx.HTTPStatusError as e:
        return UploadResult("tiktok", False,
                            error=f"HTTP {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:  # noqa: BLE001
        return UploadResult("tiktok", False, error=f"{type(e).__name__}: {e}")


def _tiktok_upload(clip: Clip, tiktok: dict | None = None) -> UploadResult:
    tt = tiktok or {}
    return tiktok_send(clip.path, title=clip.moment.title,
                       mode=str(tt.get("mode", "draft")),
                       privacy=str(tt.get("privacy_level", "SELF_ONLY")))


# -------------------------------------------------------------- Instagram ----
def _instagram_upload(clip: Clip, public_clip_url: str | None) -> UploadResult:
    """Instagram Reels needs a publicly reachable video URL (no raw file upload).

    Host the clip somewhere public and pass its URL via clip metadata / a CDN.
    """
    token = oauth.token_for("instagram")
    ig_user = oauth.record("instagram").get("ig_user_id")
    if not (token and ig_user):
        return UploadResult("instagram", False, error="not connected (Settings → Connect Instagram)")
    if not public_clip_url:
        return UploadResult("instagram", False,
                            error="instagram needs a public video URL (host the clip first)")
    try:
        create = httpx.post(
            f"https://graph.facebook.com/v20.0/{ig_user}/media",
            params={
                "media_type": "REELS",
                "video_url": public_clip_url,
                "caption": clip.moment.title,
                "access_token": token,
            },
            timeout=60,
        )
        create.raise_for_status()
        container = create.json()["id"]
        # Poll until the container finishes processing.
        for _ in range(30):
            st = httpx.get(
                f"https://graph.facebook.com/v20.0/{container}",
                params={"fields": "status_code", "access_token": token},
                timeout=30,
            ).json()
            if st.get("status_code") == "FINISHED":
                break
            time.sleep(5)
        pub = httpx.post(
            f"https://graph.facebook.com/v20.0/{ig_user}/media_publish",
            params={"creation_id": container, "access_token": token},
            timeout=60,
        )
        pub.raise_for_status()
        return UploadResult("instagram", True, url=f"ig_media:{pub.json()['id']}")
    except Exception as e:  # noqa: BLE001
        return UploadResult("instagram", False, error=str(e))


def upload_clip(
    clip: Clip,
    platforms: list[str],
    privacy: str,
    public_clip_url: str | None = None,
    tiktok: dict | None = None,
) -> list[UploadResult]:
    results: list[UploadResult] = []
    for p in platforms:
        if p == "youtube":
            results.append(_youtube_upload(clip, privacy))
        elif p == "tiktok":
            results.append(_tiktok_upload(clip, tiktok))
        elif p == "instagram":
            results.append(_instagram_upload(clip, public_clip_url))
        else:
            results.append(UploadResult(p, False, error="unknown platform"))
    return results
