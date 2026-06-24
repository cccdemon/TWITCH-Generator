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
def _tiktok_upload(clip: Clip) -> UploadResult:
    token = oauth.token_for("tiktok")
    if not token:
        return UploadResult("tiktok", False, error="not connected (Settings → Connect TikTok)")
    try:
        size = Path(clip.path).stat().st_size
        init = httpx.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={
                "post_info": {
                    "title": clip.moment.title[:150],
                    "privacy_level": "SELF_ONLY",
                    "disable_comment": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,
                    "total_chunk_count": 1,
                },
            },
            timeout=60,
        )
        init.raise_for_status()
        data = init.json()["data"]
        upload_url = data["upload_url"]
        with open(clip.path, "rb") as fh:
            put = httpx.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                content=fh.read(),
                timeout=300,
            )
        put.raise_for_status()
        return UploadResult("tiktok", True, url=f"publish_id:{data['publish_id']}")
    except Exception as e:  # noqa: BLE001
        return UploadResult("tiktok", False, error=str(e))


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
) -> list[UploadResult]:
    results: list[UploadResult] = []
    for p in platforms:
        if p == "youtube":
            results.append(_youtube_upload(clip, privacy))
        elif p == "tiktok":
            results.append(_tiktok_upload(clip))
        elif p == "instagram":
            results.append(_instagram_upload(clip, public_clip_url))
        else:
            results.append(UploadResult(p, False, error="unknown platform"))
    return results
