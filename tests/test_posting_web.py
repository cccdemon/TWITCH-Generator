import json
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from src import clip as clip_stage
from src import poster_client, posting
from src.config import data_dir

CLIP = "100_funny_x.mp4"
URL = f"/api/post/555/{CLIP}"


@pytest.fixture
def vod(monkeypatch):
    d = data_dir() / "555"
    shutil.rmtree(d, ignore_errors=True)
    (d / "clips").mkdir(parents=True)
    (d / "clips" / CLIP).write_bytes(b"ORIGINAL")
    (d / "moments.json").write_text(json.dumps(
        [{"start": 100.0, "end": 130.0, "title": "Boom", "category": "funny", "score": 0.9}]))
    (d / "info.json").write_text(json.dumps({"vod_id": "555", "duration": 140.0}))
    monkeypatch.setattr(clip_stage, "probe_duration", lambda p: 32.0)
    monkeypatch.setattr(posting, "suggest_hashtags", lambda *a, **k: ["#fail"])
    monkeypatch.setenv("POST_HASHTAGS", "#starcitizen gaming")
    monkeypatch.setenv("POST_TWITCH_URL", "twitch.tv/x")
    monkeypatch.delenv("POST_TEMPLATE", raising=False)
    monkeypatch.setenv("POSTER_URL", "http://p")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def client():
    from src import web
    c = TestClient(web.app)
    r = c.post("/login", data={"username": "t", "password": "t"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


def test_requires_login(vod):
    from src import web
    assert TestClient(web.app).get(URL).status_code == 401


def test_panel_builds_caption_from_settings(client, vod):
    j = client.get(URL).json()
    assert j["caption"] == "Boom\n\nLive: twitch.tv/x\n\n#starcitizen #gaming"
    assert j["window"]["mode"] == "clip" and j["locked"] is False and j["configured"] is True


def test_panel_ai_failure_falls_back_to_fixed(client, vod, monkeypatch):
    monkeypatch.setattr(posting, "suggest_hashtags", lambda *a, **k: [])
    assert client.post(URL + "/hashtags").json()["caption"].endswith("#starcitizen #gaming")


def test_status_offline_is_200(client, monkeypatch):
    monkeypatch.setenv("POSTER_URL", "http://p")
    monkeypatch.setattr(poster_client, "status", _raise(poster_client.PosterError("refused")))
    j = client.get("/api/poster/status").json()
    assert j["configured"] is True and j["online"] is False and "refused" in j["error"]


def test_status_unconfigured(client, monkeypatch):
    monkeypatch.delenv("POSTER_URL", raising=False)
    assert client.get("/api/poster/status").json()["configured"] is False


def test_send_rejects_over_limit_for_instagram(client, vod):
    r = client.post(URL + "/send", json={"caption": "#a " * 31, "platforms": ["instagram"]})
    assert r.status_code == 400 and "Instagram" in r.text


def test_send_records_post_and_locks_trim(client, vod, monkeypatch):
    seen = {}

    def create(path, meta):
        seen.update(meta)
        return 9

    monkeypatch.setattr(poster_client, "create", create)
    r = client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok", "instagram"],
                                         "threads": False, "dry_run": True, "run_at": 0})
    assert r.status_code == 200
    assert seen["clip_name"] == CLIP and seen["threads"] is False and seen["dry_run"] is True
    posts = posting.PostStore(vod).get(CLIP)["posts"]
    assert posts[0]["id"] == 9 and posts[0]["status"] == "queued"
    assert client.post(URL + "/trim", json={"start": 1, "end": 10}).status_code == 409


def test_send_poster_error_is_502(client, vod, monkeypatch):
    monkeypatch.setattr(poster_client, "create", _raise(poster_client.PosterError("Handy offline")))
    r = client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]})
    assert r.status_code == 502 and "Handy offline" in r.text


def test_send_missing_file_404(client, vod):
    (vod / "clips" / CLIP).unlink()
    assert client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]}).status_code == 404


def test_send_concurrent_second_is_409(client, vod, monkeypatch):
    gate, started = threading.Event(), threading.Event()

    def slow_create(path, meta):
        started.set()
        gate.wait(5)
        return 1

    monkeypatch.setattr(poster_client, "create", slow_create)
    res = {}
    t = threading.Thread(target=lambda: res.setdefault(
        "a", client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]}).status_code))
    t.start()
    assert started.wait(5)
    second = client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]}).status_code
    gate.set()
    t.join(5)
    assert (res["a"], second) == (200, 409)


def test_panel_refreshes_active_post_status(client, vod, monkeypatch):
    posting.PostStore(vod).add_post(CLIP, {"id": 4, "status": "queued", "platforms": ["tiktok"]})
    monkeypatch.setattr(poster_client, "get", lambda pid: {"id": 4, "status": "done",
                                                           "results": {"tiktok": {"ok": True}},
                                                           "shots": ["tiktok"]})
    j = client.get(URL).json()
    assert j["posts"][0]["status"] == "done" and j["locked"] is False


def test_trim_clip_mode_and_reset(client, vod, monkeypatch):
    monkeypatch.setattr(clip_stage, "trim_clip", lambda s, o, a, b: o.write_bytes(b"T") or o)
    assert client.post(URL + "/trim", json={"start": 1, "end": 10}).status_code == 200
    assert (vod / "clips" / CLIP).read_bytes() == b"T"
    assert client.post(URL + "/trim", json={"start": 1, "end": 1.2}).status_code == 400
    assert client.post(URL + "/reset").status_code == 200
    assert (vod / "clips" / CLIP).read_bytes() == b"ORIGINAL"


def test_draft_saved_and_checked(client, vod):
    j = client.put(URL + "/draft", json={"caption": "hi #a"}).json()
    assert j["check"]["hashtags"] == 1
    assert client.get(URL).json()["caption"] == "hi #a"


def test_cancel_updates_status(client, vod, monkeypatch):
    posting.PostStore(vod).add_post(CLIP, {"id": 4, "status": "queued", "platforms": ["tiktok"]})
    monkeypatch.setattr(poster_client, "cancel", lambda pid: {"ok": True})
    monkeypatch.setattr(poster_client, "get", lambda pid: {"id": 4, "status": "cancelled"})
    assert client.post(URL + "/posts/4/cancel").status_code == 200
    assert posting.PostStore(vod).get(CLIP)["posts"][0]["status"] == "cancelled"


def test_signals_relative_in_clip_mode(client, vod):
    (vod / "audio.json").write_text(json.dumps({"params": {}, "data": [[90, 0.1], [105, 0.8], [200, 1]]}))
    (vod / "transcript.json").write_text(json.dumps([{"start": 101.0, "end": 104.0, "text": "a"}]))
    j = client.get(URL + "/signals").json()
    assert j["audio"] == [[6.0, 0.8]]          # 105 - (100 - pad_start 1)
    assert j["cuts"] == [2.0, 5.0]


def test_path_traversal_rejected(client, vod):
    assert client.get("/api/post/555/..%2Finfo.json").status_code in (400, 404)
    assert client.get("/api/post/..%2F555/x.mp4").status_code in (400, 404)


def test_shot_proxied(client, monkeypatch):
    monkeypatch.setattr(poster_client, "shot", lambda pid, p: b"PNG")
    r = client.get("/api/post/shot/3/tiktok")
    assert r.status_code == 200 and r.content == b"PNG"
    assert client.get("/api/post/shot/3/evil").status_code == 404


def test_free_keeps_posting_files(client, vod):
    names = ("posts.json", "transcript.json", "audio.json", "chat.json", "facecam.json")
    for n in names:
        (vod / n).write_text("[]")
    (vod / "555.mp4").write_bytes(b"SRC")
    client.post("/vods/555/free", follow_redirects=False)
    assert all((vod / n).exists() for n in names)
    assert not (vod / "555.mp4").exists()


def test_settings_have_poster_keys():
    from src.settings_store import SETTING_KEYS
    keys = {k: secret for k, _, secret in SETTING_KEYS}
    assert keys["POSTER_TOKEN"] is True and keys["POSTER_URL"] is False
    assert {"POST_TWITCH_URL", "POST_HASHTAGS", "POST_TEMPLATE"} <= set(keys)


def test_clips_page_has_post_panel_and_free_warning(client, vod):
    (vod / "555.mp4").write_bytes(b"S" * 300_000)
    posting.PostStore(vod).add_post(CLIP, {"id": 1, "status": "done", "platforms": ["tiktok"]})
    html = client.get("/clips/555").text
    assert 'data-posting="1"' in html
    assert f'data-post-status="done"' in html
    (vod / "clips" / "200_funny_y.mp4").write_bytes(b"x")
    html = client.get("/clips/555").text
    assert "nur noch gekürzt" in html


# ---- final review fixes
def test_retry_refused_when_clip_changed_since_send(client, vod, monkeypatch):     # I1
    monkeypatch.setattr(poster_client, "create", lambda p, m: 9)
    client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]})
    store = posting.PostStore(vod)
    store.update(CLIP, posts=[{**store.get(CLIP)["posts"][0], "status": "failed"}])
    (vod / "clips" / CLIP).write_bytes(b"NEW CUT, LONGER")
    called = []
    monkeypatch.setattr(poster_client, "retry", lambda pid: called.append(pid) or {"ok": True})
    j = client.get(URL).json()
    assert j["posts"][0]["stale"] is True
    r = client.post(URL + "/posts/9/retry")
    assert r.status_code == 409 and "neu senden" in r.text and not called


def test_trim_refused_while_send_in_progress(client, vod, monkeypatch):            # I2
    gate, started = threading.Event(), threading.Event()

    def slow_create(path, meta):
        started.set(); gate.wait(5); return 1

    monkeypatch.setattr(poster_client, "create", slow_create)
    t = threading.Thread(target=lambda: client.post(URL + "/send", json={"caption": "x", "platforms": ["tiktok"]}))
    t.start()
    assert started.wait(5)
    r = client.post(URL + "/trim", json={"start": 1, "end": 10})
    gate.set(); t.join(5)
    assert r.status_code == 409


def test_refresh_does_not_drop_post_added_meanwhile(client, vod, monkeypatch):     # I3
    store = posting.PostStore(vod)
    store.add_post(CLIP, {"id": 4, "status": "queued", "platforms": ["tiktok"]})

    def get(pid):
        store.add_post(CLIP, {"id": 5, "status": "queued", "platforms": ["tiktok"]})  # concurrent send
        return {"id": 4, "status": "cancelled"}

    monkeypatch.setattr(poster_client, "get", get)
    client.get(URL)
    ids = {p["id"]: p["status"] for p in store.get(CLIP)["posts"]}
    assert ids == {4: "cancelled", 5: "queued"}


def test_panel_does_not_block_on_ai(client, vod, monkeypatch):                    # I5
    monkeypatch.setattr(posting, "suggest_hashtags", _raise(AssertionError("LLM called in panel")))
    j = client.get(URL).json()
    assert j["ai_tags"] is None and j["caption"].endswith("#starcitizen #gaming")


def test_hashtags_route_fills_ai_tags(client, vod):                               # I5
    j = client.post(URL + "/hashtags").json()
    assert j["caption"].endswith("#starcitizen #gaming #fail")
    assert posting.PostStore(vod).get(CLIP)["ai_tags"] == ["#fail"]


# ---- window preview (source segment, so the handles can go before the old start)
def test_preview_cuts_window_from_source_once(client, vod, monkeypatch):
    (vod / "555.mp4").write_bytes(b"SRC")
    calls = []

    def fake(source, out, start, end):
        calls.append((start, end)); out.write_bytes(b"PREVIEW"); return out

    monkeypatch.setattr(clip_stage, "preview_segment", fake)
    r = client.get(URL + "/preview")
    assert r.status_code == 200 and r.content == b"PREVIEW"
    assert calls == [(79.0, 140.0)]                # window min/max (pad 1, ±20 s, VOD 140 s)
    client.get(URL + "/preview")
    assert len(calls) == 1                          # cached


def test_preview_404_without_source(client, vod):
    assert client.get(URL + "/preview").status_code == 404


def test_trim_route_passes_crop(client, vod, monkeypatch):
    (vod / "555.mp4").write_bytes(b"SRC")
    seen = {}
    monkeypatch.setattr(clip_stage, "render_custom", lambda s, o, **k: seen.update(k) or o.write_bytes(b"R") or o)
    crop = {"layout": "crop", "game": {"x": 0.3, "y": 0, "w": 0.32, "h": 1.0}}
    r = client.post(URL + "/trim", json={"start": 95, "end": 125, "crop": crop})
    assert r.status_code == 200 and seen["layout"] == "crop" and r.json()["window"]["crop"] == crop


def test_panel_crop_default_uses_facecam(client, vod):
    (vod / "555.mp4").write_bytes(b"SRC")
    (vod / "facecam.json").write_text(json.dumps({"x": 0.75, "y": 0.02, "w": 0.24, "h": 0.3}))
    d = client.get(URL).json()["crop_default"]
    assert d["cam"] == {"x": 0.75, "y": 0.02, "w": 0.24, "h": 0.3} and d["layout"] in ("split", "crop")
    assert 0 < d["game"]["w"] <= 1 and d["game"]["h"] == 1
