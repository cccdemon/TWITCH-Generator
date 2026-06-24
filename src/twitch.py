"""Twitch stage: resolve VOD metadata via Helix, download video via yt-dlp."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import httpx

from .config import env
from .models import VodInfo

_VOD_ID_RE = re.compile(r"videos/(\d+)")


def parse_vod_id(vod_url: str) -> str:
    m = _VOD_ID_RE.search(vod_url)
    if m:
        return m.group(1)
    if vod_url.isdigit():
        return vod_url
    raise ValueError(f"cannot parse VOD id from: {vod_url}")


def _app_token(client_id: str, client_secret: str) -> str:
    r = httpx.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_vod_info(vod_url: str) -> VodInfo:
    """Resolve title/streamer/duration. Falls back to bare info if no creds."""
    vod_id = parse_vod_id(vod_url)
    canonical = f"https://www.twitch.tv/videos/{vod_id}"

    client_id = env("TWITCH_CLIENT_ID")
    client_secret = env("TWITCH_CLIENT_SECRET")
    if not (client_id and client_secret):
        return VodInfo(vod_id=vod_id, url=canonical)

    token = _app_token(client_id, client_secret)
    r = httpx.get(
        "https://api.twitch.tv/helix/videos",
        params={"id": vod_id},
        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json().get("data", [])
    if not items:
        return VodInfo(vod_id=vod_id, url=canonical)
    v = items[0]
    return VodInfo(
        vod_id=vod_id,
        url=canonical,
        title=v.get("title", ""),
        streamer=v.get("user_name", ""),
        duration=_parse_duration(v.get("duration", "")),
    )


def get_user_vods(login: str, limit: int = 20) -> list[VodInfo]:
    """List a streamer's archived VODs (newest first). Needs Twitch app creds."""
    client_id = env("TWITCH_CLIENT_ID", required=True)
    client_secret = env("TWITCH_CLIENT_SECRET", required=True)
    token = _app_token(client_id, client_secret)
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}

    u = httpx.get(
        "https://api.twitch.tv/helix/users",
        params={"login": login.strip().lower()},
        headers=headers, timeout=30,
    )
    u.raise_for_status()
    users = u.json().get("data", [])
    if not users:
        raise ValueError(f"no such Twitch user: {login}")
    user_id = users[0]["id"]

    r = httpx.get(
        "https://api.twitch.tv/helix/videos",
        params={"user_id": user_id, "type": "archive", "first": min(limit, 100)},
        headers=headers, timeout=30,
    )
    r.raise_for_status()
    out: list[VodInfo] = []
    for v in r.json().get("data", []):
        out.append(VodInfo(
            vod_id=v["id"],
            url=v.get("url", f"https://www.twitch.tv/videos/{v['id']}"),
            title=v.get("title", ""),
            streamer=v.get("user_name", ""),
            duration=_parse_duration(v.get("duration", "")),
        ))
    return out


def _parse_duration(s: str) -> float:
    """Twitch duration like '1h2m3s' -> seconds."""
    total = 0.0
    for value, unit in re.findall(r"(\d+)([hms])", s):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def download_vod(info: VodInfo, out_dir: Path, fmt: str = "best", reuse: bool = True) -> Path:
    """Download VOD via yt-dlp. Returns path to the video file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{info.vod_id}.mp4"
    if reuse and target.exists() and target.stat().st_size > 0:
        return target

    cmd = [
        "yt-dlp",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", str(target),
        info.url,
    ]
    subprocess.run(cmd, check=True)
    if not target.exists():
        # yt-dlp may keep source container; grab whatever it produced.
        matches = list(out_dir.glob(f"{info.vod_id}.*"))
        if not matches:
            raise RuntimeError("download produced no file")
        return matches[0]
    return target
