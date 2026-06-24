"""In-app OAuth 'Connect' flows for upload platforms (YouTube, TikTok, Instagram).

Tokens live in <data>/oauth.json and are auto-refreshed before use, so the
pipeline stays logged in without manual token pasting. The developer-app
credentials (client id/secret) still come from settings/env per platform; public
publishing still depends on each platform's app review — OAuth only removes the
manual token dance and handles refresh.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import httpx

from .config import data_dir, env

GRAPH = "https://graph.facebook.com/v20.0"
PLATFORMS = ("youtube", "tiktok", "instagram")
_LABEL = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


# --------------------------------------------------------------- store ----
def _store_path():
    return data_dir() / "oauth.json"


def _load() -> dict:
    p = _store_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(d: dict) -> None:
    _store_path().write_text(json.dumps(d, indent=2), encoding="utf-8")


def _set(platform: str, data: dict) -> None:
    d = _load()
    d[platform] = data
    _save(d)


def record(platform: str) -> dict:
    return _load().get(platform, {})


def disconnect(platform: str) -> None:
    d = _load()
    d.pop(platform, None)
    _save(d)


# ------------------------------------------------------------ config ----
def public_url() -> str | None:
    return (env("PUBLIC_URL") or "").rstrip("/") or None


def redirect_uri(platform: str) -> str | None:
    base = public_url()
    return f"{base}/oauth/{platform}/callback" if base else None


def _creds(platform: str) -> tuple[str | None, str | None]:
    if platform == "youtube":
        return env("YOUTUBE_CLIENT_ID"), env("YOUTUBE_CLIENT_SECRET")
    if platform == "tiktok":
        return env("TIKTOK_CLIENT_KEY"), env("TIKTOK_CLIENT_SECRET")
    if platform == "instagram":
        return env("FACEBOOK_APP_ID"), env("FACEBOOK_APP_SECRET")
    return None, None


def configured(platform: str) -> bool:
    cid, sec = _creds(platform)
    return bool(cid and sec and public_url())


def status(platform: str) -> dict:
    rec = record(platform)
    return {
        "platform": platform,
        "label": _LABEL[platform],
        "configured": configured(platform),
        "connected": bool(rec.get("access_token")),
        "account": rec.get("account", ""),
    }


def all_status() -> list[dict]:
    return [status(p) for p in PLATFORMS]


# ----------------------------------------------------------- auth URL ----
def build_auth_url(platform: str, state: str) -> str:
    cid, _ = _creds(platform)
    ru = redirect_uri(platform)
    if platform == "youtube":
        params = {
            "client_id": cid, "redirect_uri": ru, "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube.upload "
                     "https://www.googleapis.com/auth/youtube.readonly",
            "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state,
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    if platform == "tiktok":
        params = {
            "client_key": cid, "redirect_uri": ru, "response_type": "code",
            "scope": "user.info.basic,video.publish", "state": state,
        }
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)
    if platform == "instagram":
        params = {
            "client_id": cid, "redirect_uri": ru, "response_type": "code",
            "scope": "instagram_basic,instagram_content_publish,"
                     "pages_show_list,pages_read_engagement,business_management",
            "state": state,
        }
        return "https://www.facebook.com/v20.0/dialog/oauth?" + urlencode(params)
    raise ValueError(f"unknown platform: {platform}")


# ----------------------------------------------------- code exchange ----
def exchange_code(platform: str, code: str) -> None:
    cid, sec = _creds(platform)
    ru = redirect_uri(platform)
    if platform == "youtube":
        t = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": cid, "client_secret": sec,
            "redirect_uri": ru, "grant_type": "authorization_code",
        }, timeout=30).raise_for_status().json()
        _set("youtube", {
            "access_token": t["access_token"],
            "refresh_token": t.get("refresh_token"),
            "expires_at": time.time() + int(t.get("expires_in", 3600)),
            "account": _youtube_channel(t["access_token"]),
        })
    elif platform == "tiktok":
        t = httpx.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": cid, "client_secret": sec, "code": code,
            "grant_type": "authorization_code", "redirect_uri": ru,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30).raise_for_status().json()
        _set("tiktok", {
            "access_token": t["access_token"],
            "refresh_token": t.get("refresh_token"),
            "expires_at": time.time() + int(t.get("expires_in", 86400)),
            "open_id": t.get("open_id"),
            "account": t.get("open_id", "TikTok"),
        })
    elif platform == "instagram":
        short = httpx.get(GRAPH + "/oauth/access_token", params={
            "client_id": cid, "client_secret": sec, "redirect_uri": ru, "code": code,
        }, timeout=30).raise_for_status().json()["access_token"]
        lt = httpx.get(GRAPH + "/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": cid,
            "client_secret": sec, "fb_exchange_token": short,
        }, timeout=30).raise_for_status().json()
        token = lt["access_token"]
        ig_user, page, name = _ig_account(token)
        _set("instagram", {
            "access_token": token,
            "expires_at": time.time() + int(lt.get("expires_in", 60 * 86400)),
            "ig_user_id": ig_user, "page_id": page, "account": name,
        })
    else:
        raise ValueError(f"unknown platform: {platform}")


# -------------------------------------------------------- refresh ----
def token_for(platform: str) -> str | None:
    """Valid access token for a connected platform, refreshing if near expiry."""
    rec = record(platform)
    if not rec.get("access_token"):
        return None
    if rec.get("expires_at", 0) - time.time() > 120:
        return rec["access_token"]
    try:
        return _refresh(platform, rec)
    except Exception:  # noqa: BLE001
        return rec.get("access_token")  # stale but better than nothing


def _refresh(platform: str, rec: dict) -> str | None:
    cid, sec = _creds(platform)
    if platform == "youtube":
        if not rec.get("refresh_token"):
            return rec.get("access_token")
        t = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": cid, "client_secret": sec,
            "refresh_token": rec["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30).raise_for_status().json()
        rec["access_token"] = t["access_token"]
        rec["expires_at"] = time.time() + int(t.get("expires_in", 3600))
        _set("youtube", rec)
        return rec["access_token"]
    if platform == "tiktok":
        t = httpx.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": cid, "client_secret": sec,
            "refresh_token": rec.get("refresh_token", ""), "grant_type": "refresh_token",
        }, headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30).raise_for_status().json()
        rec["access_token"] = t["access_token"]
        rec["refresh_token"] = t.get("refresh_token", rec.get("refresh_token"))
        rec["expires_at"] = time.time() + int(t.get("expires_in", 86400))
        _set("tiktok", rec)
        return rec["access_token"]
    if platform == "instagram":
        # FB long-lived tokens have no refresh_token; re-exchange to extend 60d.
        lt = httpx.get(GRAPH + "/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": cid,
            "client_secret": sec, "fb_exchange_token": rec["access_token"],
        }, timeout=30).raise_for_status().json()
        rec["access_token"] = lt["access_token"]
        rec["expires_at"] = time.time() + int(lt.get("expires_in", 60 * 86400))
        _set("instagram", rec)
        return rec["access_token"]
    return rec.get("access_token")


# -------------------------------------------------------- helpers ----
def _youtube_channel(token: str) -> str:
    try:
        r = httpx.get("https://www.googleapis.com/youtube/v3/channels", params={
            "part": "snippet", "mine": "true",
        }, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        items = r.json().get("items", [])
        return items[0]["snippet"]["title"] if items else "YouTube"
    except Exception:  # noqa: BLE001
        return "YouTube"


def _ig_account(token: str) -> tuple[str | None, str | None, str]:
    """Resolve the IG business user id + username from a FB user token."""
    try:
        pages = httpx.get(GRAPH + "/me/accounts", params={
            "access_token": token,
        }, timeout=30).json().get("data", [])
        if not pages:
            return None, None, "Instagram (no page)"
        page = pages[0]["id"]
        r = httpx.get(f"{GRAPH}/{page}", params={
            "fields": "instagram_business_account{id,username}",
            "access_token": token,
        }, timeout=30).json()
        iba = r.get("instagram_business_account") or {}
        return iba.get("id"), page, ("@" + iba.get("username", "instagram"))
    except Exception:  # noqa: BLE001
        return None, None, "Instagram"
