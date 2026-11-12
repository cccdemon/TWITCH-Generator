import httpx
import pytest

from src import poster_client as pc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("POSTER_URL", "http://p:3300/")
    monkeypatch.setenv("POSTER_TOKEN", "tok")
    yield
    pc._transport = None


def _mock(monkeypatch, handler):
    monkeypatch.setattr(pc, "_transport", httpx.MockTransport(handler))


def test_configured_needs_url(monkeypatch):
    assert pc.configured() is True
    monkeypatch.delenv("POSTER_URL")
    assert pc.configured() is False


def test_create_sends_bearer_and_meta(tmp_path, monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.read()
        return httpx.Response(200, json={"id": 5})

    _mock(monkeypatch, handler)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"xx")
    assert pc.create(f, {"platforms": ["tiktok"], "caption": "c"}) == 5
    assert seen["url"] == "http://p:3300/api/posts"
    assert seen["auth"] == "Bearer tok"
    assert b'"platforms"' in seen["body"] and b'filename="a.mp4"' in seen["body"]


def test_http_error_detail_becomes_postererror(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(409, json={"detail": "nur geplante Posts sind änderbar"}))
    with pytest.raises(pc.PosterError, match="nur geplante") as e:
        pc.edit(1, caption="x")
    assert e.value.status == 409


def test_unreachable_is_postererror(monkeypatch):
    def boom(r):
        raise httpx.ConnectError("refused")

    _mock(monkeypatch, boom)
    with pytest.raises(pc.PosterError):
        pc.status()


def test_shot_returns_bytes(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, content=b"PNG"))
    assert pc.shot(3, "tiktok") == b"PNG"


def test_non_json_reply_is_postererror(monkeypatch):                                # review M1
    _mock(monkeypatch, lambda r: httpx.Response(200, text="<html>router login</html>"))
    with pytest.raises(pc.PosterError):
        pc.status()


def test_create_reply_without_id_is_postererror(tmp_path, monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"nope": 1}))
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    with pytest.raises(pc.PosterError):
        pc.create(f, {})
