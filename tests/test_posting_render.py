import json
import shutil

import pytest

from src import clip as clip_stage
from src.posting import LockedError, PostStore, apply_trim, reset_trim, window

CLIP = "100_funny_x.mp4"
MOMENTS = {100: {"start": 100.0, "end": 130.0, "title": "Boom", "category": "funny"}}
CFG = {"clip": {"pad_start": 1, "pad_end": 1, "width": 1080, "height": 1920,
                "layout": "split", "burn_subtitles": False}}


@pytest.fixture
def vod(tmp_path):
    d = tmp_path / "555"
    (d / "clips").mkdir(parents=True)
    (d / "clips" / CLIP).write_bytes(b"ORIGINAL")
    (d / "555.mp4").write_bytes(b"SRC")
    (d / "info.json").write_text(json.dumps({"vod_id": "555", "duration": 140.0}))
    return d


@pytest.fixture
def vod_no_src(vod):
    (vod / "555.mp4").unlink()
    return vod


@pytest.fixture
def rec(monkeypatch):
    calls = {}

    def fake_render(source, out_path, **kw):
        calls["render_custom"] = {"source": source, "out": out_path, **kw}
        out_path.write_bytes(b"RENDERED")
        return out_path

    def fake_trim(src, out, start, end):
        calls.setdefault("trim", []).append((start, end))
        out.write_bytes(b"TRIMMED")
        return out

    monkeypatch.setattr(clip_stage, "render_custom", fake_render)
    monkeypatch.setattr(clip_stage, "trim_clip", fake_trim)
    monkeypatch.setattr(clip_stage, "probe_duration", lambda p: 32.0)
    return calls


def test_window_source_margins_clamped(vod, rec):
    w = window(vod, CLIP, MOMENTS, CFG)
    assert (w["kind"], w["mode"], w["source"]) == ("pipeline", "source", True)
    assert (w["start"], w["end"], w["min"], w["max"]) == (99, 131, 79, 140)
    assert (w["orig_start"], w["orig_end"]) == (99, 131)


def test_window_uses_stored_trim(vod, rec):
    PostStore(vod).update(CLIP, trim={"mode": "source", "start": 90, "end": 120})
    w = window(vod, CLIP, MOMENTS, CFG)
    assert (w["start"], w["end"], w["min"], w["max"]) == (90, 120, 70, 140)


def test_window_without_source_is_clip_mode(vod_no_src, rec):
    w = window(vod_no_src, CLIP, MOMENTS, CFG)
    assert (w["mode"], w["start"], w["end"], w["min"], w["max"]) == ("clip", 0, 32.0, 0, 32.0)


def test_window_edit_clip_is_clip_mode_even_with_source(vod, rec):
    shutil.copy(vod / "clips" / CLIP, vod / "clips" / "100_edit_y.mp4")
    assert window(vod, "100_edit_y.mp4", MOMENTS, CFG)["mode"] == "clip"


def test_apply_trim_source_calls_render_custom_and_stores(vod, rec):
    cam = {"x": 0, "y": 0, "w": .2, "h": .2}
    w = apply_trim(vod, CLIP, 95, 125, MOMENTS, CFG, facecam=cam, segments=None)
    r = rec["render_custom"]
    assert r["start"] == 95 and r["duration"] == 30 and r["game"] is None
    assert r["facecam"] == cam and r["layout"] == "split" and r["title"] == "Boom"
    assert r["out"] == vod / "clips" / CLIP
    assert PostStore(vod).get(CLIP)["trim"] == {"mode": "source", "start": 95, "end": 125}
    assert (w["start"], w["end"]) == (95, 125)


def test_apply_trim_locked(vod, rec):
    PostStore(vod).add_post(CLIP, {"id": 1, "status": "queued"})
    with pytest.raises(LockedError):
        apply_trim(vod, CLIP, 95, 125, MOMENTS, CFG, None, None)
    with pytest.raises(LockedError):
        reset_trim(vod, CLIP, MOMENTS, CFG, None, None)


def test_too_short_or_out_of_range_rejected(vod, rec):
    with pytest.raises(ValueError):
        apply_trim(vod, CLIP, 100, 100.5, MOMENTS, CFG, None, None)
    with pytest.raises(ValueError):
        apply_trim(vod, CLIP, 50, 120, MOMENTS, CFG, None, None)


def test_clip_mode_backup_once_and_reset(vod_no_src, rec):
    apply_trim(vod_no_src, CLIP, 2, 20, MOMENTS, CFG, None, None)
    apply_trim(vod_no_src, CLIP, 1, 10, MOMENTS, CFG, None, None)
    orig = vod_no_src / "clips" / ".orig" / CLIP
    assert orig.read_bytes() == b"ORIGINAL"
    assert rec["trim"] == [(2, 20), (1, 10)]
    assert PostStore(vod_no_src).get(CLIP)["trim"] is None
    reset_trim(vod_no_src, CLIP, MOMENTS, CFG, None, None)
    assert (vod_no_src / "clips" / CLIP).read_bytes() == b"ORIGINAL"
    assert not orig.exists()


def test_reset_clip_mode_without_backup_fails(vod_no_src, rec):
    with pytest.raises(ValueError):
        reset_trim(vod_no_src, CLIP, MOMENTS, CFG, None, None)


def test_reset_source_renders_original_window(vod, rec):
    PostStore(vod).update(CLIP, trim={"mode": "source", "start": 90, "end": 120})
    reset_trim(vod, CLIP, MOMENTS, CFG, None, None)
    assert rec["render_custom"]["start"] == 99 and rec["render_custom"]["duration"] == 32
    assert PostStore(vod).get(CLIP)["trim"] is None


def test_window_clamps_with_probed_source_when_info_lacks_duration(vod, rec, monkeypatch):
    (vod / "info.json").write_text(json.dumps({"vod_id": "555"}))
    monkeypatch.setattr(clip_stage, "probe_duration", lambda p: 135.0 if p.name == "555.mp4" else 32.0)
    assert window(vod, CLIP, MOMENTS, CFG)["max"] == 135.0
