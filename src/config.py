"""Config loading: YAML tunables + env-var secrets."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def data_dir() -> Path:
    d = Path(os.environ.get("TG_DATA_DIR", "./data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def root_path() -> str:
    """External URL prefix the app is served under (e.g. '/vod'). '' = root.

    The reverse proxy strips this prefix before requests reach the app, so routes
    stay unprefixed; this value is only used to build outward links/redirects.
    """
    rp = os.environ.get("ROOT_PATH", "").strip().rstrip("/")
    if rp and not rp.startswith("/"):
        rp = "/" + rp
    return rp


def load_config(path: str | None = None) -> dict[str, Any]:
    cfg_path = Path(path or os.environ.get("TG_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def env(name: str, required: bool = False, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {name}")
    return val
