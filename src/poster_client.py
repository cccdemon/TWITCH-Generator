"""HTTP client for the clip-poster phone service (poster/).

POSTER_URL / POSTER_TOKEN are read from the environment on every call (the
Settings page can change them at runtime). The token never reaches the browser.
Every failure, network or HTTP, surfaces as PosterError with a readable message.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

_transport: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport


class PosterError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def configured() -> bool:
    return bool((os.environ.get("POSTER_URL") or "").strip())


def _request(method: str, path: str, *, timeout: float = 10.0, **kw) -> httpx.Response:
    base = (os.environ.get("POSTER_URL") or "").strip().rstrip("/")
    if not base:
        raise PosterError("Handy-Dienst nicht eingerichtet (POSTER_URL)")
    headers = {"Authorization": f"Bearer {os.environ.get('POSTER_TOKEN', '')}"}
    try:
        with httpx.Client(transport=_transport, timeout=timeout) as c:
            r = c.request(method, base + path, headers=headers, **kw)
    except httpx.HTTPError as e:
        raise PosterError(f"Handy-Dienst nicht erreichbar: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise PosterError(str(detail or r.text[:300] or f"HTTP {r.status_code}"), r.status_code)
    return r


def _json(r: httpx.Response) -> dict:
    try:
        d = r.json()
    except ValueError as e:
        raise PosterError(f"Handy-Dienst: unerwartete Antwort ({r.text[:80]!r}) – URL prüfen") from e
    if not isinstance(d, dict):
        raise PosterError("Handy-Dienst: unerwartete Antwort")
    return d


def status() -> dict:
    return _json(_request("GET", "/api/status"))


def create(video: Path, meta: dict) -> int:
    with Path(video).open("rb") as fh:
        r = _request("POST", "/api/posts", timeout=300.0,
                     files={"video": (Path(video).name, fh, "video/mp4")},
                     data={"meta": json.dumps(meta)})
    try:
        return int(_json(r)["id"])
    except (KeyError, TypeError, ValueError) as e:
        raise PosterError("Handy-Dienst: Antwort ohne Post-ID") from e


def get(pid: int) -> dict:
    return _json(_request("GET", f"/api/posts/{int(pid)}"))


def edit(pid: int, **fields) -> dict:
    return _json(_request("PATCH", f"/api/posts/{int(pid)}", json=fields))


def cancel(pid: int) -> dict:
    return _json(_request("POST", f"/api/posts/{int(pid)}/cancel"))


def retry(pid: int) -> dict:
    return _json(_request("POST", f"/api/posts/{int(pid)}/retry"))


def shot(pid: int, platform: str) -> bytes:
    return _request("GET", f"/api/posts/{int(pid)}/shot/{platform}").content
