"""Render-at-post: a pipeline clip starts as a raw piece of the VOD (clips/raw/) and is
rendered from the post panel. The source VOD may already be gone."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from src import clip as clip_stage
from src import posting
from src.config import data_dir

CLIP = "100_funny_x.mp4"
URL = f"/api/post/555/{CLIP}"
CFG = {"clip": {"pad_start": 1, "pad_end": 1, "width": 1080, "height": 1920, "layout": "split"}}
MOMENTS = {100: {"start": 100.0, "end": 130.0, "title": "Boom", "category": "funny"}}


@pytest.fixture
def vod(monkeypatch):
    """VOD 555 with one raw piece covering 78.0..151.5 s, nothing rendered, source freed."""
    d = data_dir() / "555"
    shutil.rmtree(d, ignore_errors=True)
    (d / "clips" / "raw").mkdir(parents=True)
    (d / "clips" / "raw" / CLIP).write_bytes(b"RAW")
    (d / "clips" / "raw" / CLIP).with_suffix(".json").write_text(json.dumps({"start": 78.0, "end": 151.5}))
    (d / "moments.json").write_text(json.dumps(
        [{"start": 100.0, "end": 130.0, "title": "Boom", "category": "funny", "score": 0.9}]))
    (d / "info.json").write_text(json.dumps({"vod_id": "555", "duration": 400.0}))
    monkeypatch.setattr(posting, "suggest_hashtags", lambda *a, **k: [])
    monkeypatch.setenv("POSTER_URL", "http://p")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def rec(monkeypatch):
    calls = {}

    def fake_render(source, out_path, **kw):
        calls["render"] = {"source": source, **kw}
        out_path.write_bytes(b"RENDERED")
        return out_path

    monkeypatch.setattr(clip_stage, "render_custom", fake_render)
    return calls


@pytest.fixture
def client():
    from src import web
    c = TestClient(web.app)
    assert c.post("/login", data={"username": "t", "password": "t"}, follow_redirects=False).status_code == 303
    return c


def test_window_spans_the_raw_piece_and_is_unrendered(vod):
    w = posting.window(vod, CLIP, MOMENTS, CFG)
    assert (w["mode"], w["raw"], w["rendered"], w["source"]) == ("source", True, False, False)
    assert (w["min"], w["max"]) == (78.0, 151.5)
    assert (w["start"], w["end"]) == (99.0, 131.0)        # the moment with its padding


def test_render_reads_the_raw_piece_at_its_offset(vod, rec):
    w = posting.apply_trim(vod, CLIP, 90.0, 120.0, MOMENTS, CFG, None, None)
    r = rec["render"]
    assert r["source"] == posting.raw_path(vod, CLIP)
    assert r["start"] == 90.0 and r["seek"] == 12.0 and r["duration"] == 30.0
    assert w["rendered"] is True and (w["start"], w["end"]) == (90.0, 120.0)
    assert (w["min"], w["max"]) == (78.0, 151.5)          # range stays the piece


def test_cut_outside_the_raw_piece_is_rejected(vod, rec):
    with pytest.raises(ValueError):
        posting.apply_trim(vod, CLIP, 70.0, 120.0, MOMENTS, CFG, None, None)
    assert "render" not in rec


def test_reset_goes_back_to_the_raw_piece(vod, rec):
    crop = {"layout": "crop", "game": {"x": 0.3, "y": 0, "w": 0.32, "h": 1.0}}
    posting.apply_trim(vod, CLIP, 90.0, 120.0, MOMENTS, CFG, None, None, crop=crop)
    assert posting.is_rendered(vod, CLIP)
    w = posting.reset_trim(vod, CLIP, MOMENTS, CFG, None, None)
    assert not posting.is_rendered(vod, CLIP) and posting.raw_path(vod, CLIP).is_file()
    assert w["rendered"] is False and w["crop"] is None and (w["start"], w["end"]) == (99.0, 131.0)


def test_raw_piece_without_sidecar_does_not_count(vod):
    posting.raw_path(vod, CLIP).with_suffix(".json").unlink()
    assert posting.raw_info(vod, CLIP) is None and not posting.clip_exists(vod, CLIP)


def test_panel_and_listing_show_the_unrendered_clip(client, vod):
    j = client.get(URL).json()
    assert j["window"]["rendered"] is False and j["posts"] == []
    clips = client.get("/api/clips/555").json()["clips"]
    assert [(c["name"], c["rendered"], c["raw"]) for c in clips] == [(CLIP, False, True)]
    assert client.get("/clips/555").status_code == 200


def test_clip_url_serves_raw_until_rendered(client, vod, rec):
    assert client.get(f"/clips/555/{CLIP}").content == b"RAW"
    assert client.post(URL + "/trim", json={"start": 90, "end": 120}).status_code == 200
    assert client.get(f"/clips/555/{CLIP}").content == b"RENDERED"
    assert client.get(f"/clips/555/{CLIP}?raw=1").content == b"RAW"
    assert client.get(URL + "/preview").content == b"RAW"   # preview = the piece itself


def test_send_needs_a_render_first(client, vod):
    r = client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]})
    assert r.status_code == 400 and "gerendert" in r.json()["detail"]


def test_cut_raw_starts_on_the_keyframe_and_writes_the_sidecar(tmp_path, monkeypatch):
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[0] == "ffprobe":
            class R:  # keyframes every 2 s; csv output has a trailing comma on some builds
                stdout = "96.000000,\n98.000000,\n100.000000,\n"
            return R()
        (tmp_path / "raw" / "n.mp4.part").write_bytes(b"x")

    monkeypatch.setattr(clip_stage.subprocess, "run", fake_run)
    monkeypatch.setattr(clip_stage, "probe_duration", lambda p: 61.5)
    info = clip_stage.cut_raw(tmp_path / "v.mp4", tmp_path / "raw", "n", 99.3, 160.0)
    assert info == {"start": 98.0, "end": 159.5}
    ff = cmds[-1]
    assert ff[ff.index("-ss") + 1] == "98.000" and ff[ff.index("-c") + 1] == "copy"
    assert json.loads((tmp_path / "raw" / "n.json").read_text()) == info


def test_is_fragmented_reads_the_box_layout(tmp_path):
    def box(typ, payload=b""):
        return (8 + len(payload)).to_bytes(4, "big") + typ + payload

    frag, plain = tmp_path / "f.mp4", tmp_path / "p.mp4"
    frag.write_bytes(box(b"ftyp", b"isom") + box(b"moov", b"x" * 20) + box(b"moof", b"y") + box(b"mdat", b"z"))
    plain.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"z" * 50) + box(b"moov", b"x"))
    assert clip_stage.is_fragmented(frag) and not clip_stage.is_fragmented(plain)
    assert not clip_stage.is_fragmented(tmp_path / "missing.mp4")
