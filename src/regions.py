"""User-picked picture regions for the vertical clip: facecam box, content box, layout.

One spec format everywhere (same as the per-clip crop in posts.json):

    {"layout": "split"|"crop", "cam": box, "game": box|None, "top": 0.15..0.75}

Boxes are fractions {x,y,w,h} of the source frame. `top` is the facecam cell's share
of the output height; the picker derives it from the facecam box's shape, so a wide
box gives a flat cell and the content gets the rest.

Stored per VOD in `facecam.json` (cam keys stay at the top level so vision's facecam
cache and older readers keep working, plus `game`, `layout`, `top`, `manual: true`)
and optionally as the default for VODs without their own pick in
`<data>/layout_default.json`. Pure logic, no web or ffmpeg imports.
"""
from __future__ import annotations

import json
from pathlib import Path

from .posting import LAYOUTS, _box

DEFAULT_NAME = "layout_default.json"
TOP_MIN, TOP_MAX = 0.15, 0.75


def clamp_top(top) -> float:
    try:
        return round(max(TOP_MIN, min(TOP_MAX, float(top))), 4)
    except (TypeError, ValueError):
        return 0.5


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) and d else None


def _spec(d: dict | None, default_layout: str = "split") -> dict | None:
    """File content -> spec, or None when it holds no usable facecam box."""
    if not d:
        return None
    try:
        cam = _box(d)
    except ValueError:
        return None
    game = None
    if d.get("game"):
        try:
            game = _box(d["game"])
        except ValueError:
            game = None
    layout = d.get("layout") if d.get("layout") in LAYOUTS else default_layout
    return {"layout": layout if layout in LAYOUTS else "split", "cam": cam, "game": game,
            "top": clamp_top(d.get("top", 0.5))}


def load_vod(vod_dir: Path, default_layout: str = "split") -> dict | None:
    """The regions picked by hand for this VOD (an auto-detected box does not count)."""
    d = _read(Path(vod_dir) / "facecam.json")
    return _spec(d, default_layout) if d and d.get("manual") else None


def load_default(data_root: Path, default_layout: str = "split") -> dict | None:
    return _spec(_read(Path(data_root) / DEFAULT_NAME), default_layout)


def resolve(vod_dir: Path, default_layout: str = "split") -> dict | None:
    """VOD pick, else the saved default, else None (caller falls back to auto-detect)."""
    vod_dir = Path(vod_dir)
    return load_vod(vod_dir, default_layout) or load_default(vod_dir.parent, default_layout)


def save(vod_dir: Path, spec: dict, as_default: bool = False) -> dict:
    """Validate and store a spec for the VOD (and as the default). Raises ValueError."""
    if not isinstance(spec, dict) or spec.get("layout") not in LAYOUTS:
        raise ValueError("Bildbereiche: Layout muss split oder crop sein")
    cam, game = _box(spec.get("cam")), _box(spec.get("game"))
    rnd = lambda b: {k: round(v, 4) for k, v in b.items()}  # noqa: E731
    out = {**rnd(cam), "manual": True, "layout": spec["layout"], "game": rnd(game),
           "top": clamp_top(spec.get("top", 0.5))}
    vod_dir = Path(vod_dir)
    text = json.dumps(out)
    (vod_dir / "facecam.json").write_text(text, encoding="utf-8")
    if as_default:
        (vod_dir.parent / DEFAULT_NAME).write_text(text, encoding="utf-8")
    return _spec(out)
