"""Picture regions (facecam box, content box, split ratio): store, resolve, render graph."""
import json

import pytest

from src import clip as clip_stage
from src import regions
from src.posting import PostStore, apply_trim, reset_trim

CLIP = "100_funny_x.mp4"
MOMENTS = {100: {"start": 100.0, "end": 130.0, "title": "Boom", "category": "funny"}}
CFG = {"clip": {"pad_start": 1, "pad_end": 1, "width": 1080, "height": 1920,
                "layout": "split", "burn_subtitles": False}}
SPEC = {"layout": "split", "cam": {"x": 0.7, "y": 0.0, "w": 0.3, "h": 0.2},
        "game": {"x": 0.2, "y": 0.0, "w": 0.5, "h": 1.0}, "top": 0.3333}


@pytest.fixture
def vod(tmp_path):
    d = tmp_path / "555"
    (d / "clips").mkdir(parents=True)
    (d / "clips" / CLIP).write_bytes(b"ORIGINAL")
    (d / "555.mp4").write_bytes(b"SRC")
    (d / "info.json").write_text(json.dumps({"vod_id": "555", "duration": 140.0}))
    return d


@pytest.fixture
def rec(monkeypatch):
    calls = {}

    def fake_render(source, out_path, **kw):
        calls["render_custom"] = kw
        out_path.write_bytes(b"RENDERED")
        return out_path

    monkeypatch.setattr(clip_stage, "render_custom", fake_render)
    monkeypatch.setattr(clip_stage, "probe_duration", lambda p: 32.0)
    return calls


def test_save_and_load_vod_keeps_cam_keys_at_top_level(vod):
    out = regions.save(vod, SPEC)
    assert out == SPEC
    raw = json.loads((vod / "facecam.json").read_text())
    assert raw["manual"] is True and (raw["x"], raw["w"]) == (0.7, 0.3)  # vision's cache format
    assert regions.load_vod(vod) == SPEC
    assert not (vod.parent / regions.DEFAULT_NAME).exists()


def test_default_applies_to_vods_without_own_pick(vod, tmp_path):
    regions.save(vod, SPEC, as_default=True)
    other = tmp_path / "777"
    other.mkdir()
    assert regions.resolve(other) == SPEC
    # an auto-detected box is not a pick: the default still wins
    (other / "facecam.json").write_text(json.dumps({"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}))
    assert regions.resolve(other) == SPEC
    # the VOD's own pick beats the default
    own = {**SPEC, "top": 0.5}
    regions.save(other, own)
    assert regions.resolve(other) == own


def test_resolve_none_without_pick_or_default(vod):
    assert regions.resolve(vod) is None


def test_old_manual_facecam_without_game_still_resolves(vod):
    (vod / "facecam.json").write_text(json.dumps({"x": 0.7, "y": 0, "w": 0.3, "h": 0.3, "manual": True}))
    assert regions.resolve(vod, "split") == {"layout": "split", "game": None, "top": 0.5,
                                             "cam": {"x": 0.7, "y": 0.0, "w": 0.3, "h": 0.3}}


@pytest.mark.parametrize("bad", [
    {"layout": "wild", "cam": SPEC["cam"], "game": SPEC["game"]},
    {"layout": "split", "cam": {"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}, "game": SPEC["game"]},
    {"layout": "split", "cam": SPEC["cam"]},
])
def test_save_rejects_bad_spec(vod, bad):
    with pytest.raises(ValueError):
        regions.save(vod, bad)
    assert not (vod / "facecam.json").exists()


def test_top_is_clamped(vod):
    assert regions.save(vod, {**SPEC, "top": 0.99})["top"] == regions.TOP_MAX
    assert regions.save(vod, {**SPEC, "top": 0.01})["top"] == regions.TOP_MIN


def test_stack_graph_cell_heights_follow_top():
    g = clip_stage._stack_graph(1080, 1920, SPEC["cam"], SPEC["game"], 1 / 3)
    assert "scale=1080:640:" in g and "scale=1080:1280:" in g and "flags=lanczos" in g
    top, bot = g.split("[top];")
    assert "unsharp" in top and "unsharp" not in bot      # only the facecam is sharpened
    assert "scale=1080:960:" in clip_stage._stack_graph(1080, 1920, None, None)   # default = halves


def test_recut_without_clip_crop_uses_vod_regions(vod, rec):
    regions.save(vod, SPEC)
    apply_trim(vod, CLIP, 95, 125, MOMENTS, CFG, facecam={"x": 0, "y": 0, "w": .2, "h": .2}, segments=None)
    r = rec["render_custom"]
    assert (r["facecam"], r["game"], r["top"], r["layout"]) == (SPEC["cam"], SPEC["game"], SPEC["top"], "split")


def test_clip_crop_beats_vod_regions_and_carries_top(vod, rec):
    regions.save(vod, SPEC)
    crop = {"layout": "split", "cam": {"x": 0.0, "y": 0.0, "w": 0.4, "h": 0.4},
            "game": {"x": 0.1, "y": 0.0, "w": 0.6, "h": 0.9}, "top": 0.4}
    apply_trim(vod, CLIP, 95, 125, MOMENTS, CFG, None, None, crop=crop)
    assert rec["render_custom"]["top"] == 0.4 and rec["render_custom"]["facecam"] == crop["cam"]
    assert PostStore(vod).get(CLIP)["crop"] == crop
    reset_trim(vod, CLIP, MOMENTS, CFG, None, None)       # reset falls back to the VOD regions
    assert rec["render_custom"]["top"] == SPEC["top"] and PostStore(vod).get(CLIP)["crop"] is None
