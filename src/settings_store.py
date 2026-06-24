"""Persisted runtime settings (the values that otherwise live in .env).

The web UI writes these to <data>/settings.json. apply_settings() pushes them into
os.environ so every stage (which reads env via config.env) picks them up. File values
override the baseline .env so the UI is the source of truth once used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import data_dir

# Editable keys exposed in the web UI. (key, label, secret?)
SETTING_KEYS: list[tuple[str, str, bool]] = [
    ("ANTHROPIC_API_KEY", "Anthropic API Key", True),
    ("TWITCH_CLIENT_ID", "Twitch Client ID", False),
    ("TWITCH_CLIENT_SECRET", "Twitch Client Secret", True),
    # Public base URL of this app (OAuth redirect target), e.g.
    # https://suite.raumdock.org/vod
    ("PUBLIC_URL", "Public base URL (for OAuth redirects)", False),
    # Developer-app credentials for the in-app OAuth Connect flows.
    ("YOUTUBE_CLIENT_ID", "YouTube/Google OAuth Client ID", False),
    ("YOUTUBE_CLIENT_SECRET", "YouTube/Google OAuth Client Secret", True),
    ("TIKTOK_CLIENT_KEY", "TikTok Client Key", False),
    ("TIKTOK_CLIENT_SECRET", "TikTok Client Secret", True),
    ("FACEBOOK_APP_ID", "Facebook App ID (Instagram)", False),
    ("FACEBOOK_APP_SECRET", "Facebook App Secret (Instagram)", True),
]

_KEYS = {k for k, _, _ in SETTING_KEYS}


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> dict[str, str]:
    p = settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_settings(values: dict[str, str]) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Keep only known keys, drop blanks (blank = "leave unset / use .env").
    clean = {k: v for k, v in values.items() if k in _KEYS and v.strip()}
    p.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    apply_settings(clean)


def apply_settings(values: dict[str, str] | None = None) -> None:
    values = values if values is not None else load_settings()
    for k, v in values.items():
        if k in _KEYS and v:
            os.environ[k] = v


def current_view() -> list[dict]:
    """Settings for the UI: value shown only for non-secrets; secrets just 'set?'."""
    stored = load_settings()
    out = []
    for key, label, secret in SETTING_KEYS:
        val = stored.get(key) or os.environ.get(key, "")
        out.append({
            "key": key,
            "label": label,
            "secret": secret,
            "is_set": bool(val),
            "value": "" if secret else val,
        })
    return out
