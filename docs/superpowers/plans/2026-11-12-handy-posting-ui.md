# Handy-Posting-UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clip-Seite bekommt pro Clip ein „📱 Posten"-Feld: grafisch schneiden, Vorschau, vorgeschlagener Text (feste + KI-Hashtags, Twitch-Link), TikTok/Instagram/Threads, sofort oder geplant, Status vom Handy-Dienst `poster/`.

**Architecture:** Reine Logik (Text, Limits, `posts.json`, Fenster) in `src/posting.py`; HTTP zum Handy-Dienst in `src/poster_client.py` (Token bleibt serverseitig); alle neuen Endpunkte als `APIRouter` in `src/posting_web.py`, eingebunden in `src/web.py`. UI inline in `src/templates/clips_vod.html` (kein Static-Ordner). Rendern über die vorhandenen `clip.render_custom` + neues `clip.trim_clip`.

**Tech Stack:** Python 3.11, FastAPI, httpx, Jinja2, ffmpeg, Vanilla JS; Tests mit pytest + FastAPI `TestClient` + `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-11-12-handy-posting-ui-design.md`

## Global Constraints

- Plattform-Grenzen exakt wie im Dienst: Instagram ≤ 2200 Zeichen und ≤ 30 `#` (`caption.count("#")`), TikTok ≤ 4000 Zeichen. Überschreitung sperrt Senden für die betroffene Plattform.
- Gesendet wird immer die aktuelle Clip-Datei auf der Platte = das, was die Render-Vorschau zeigt.
- Schneiden/Rendern/Zurücksetzen gesperrt (HTTP 409, Text „erst Post abbrechen"), solange ein Post des Clips `queued` oder `running` ist.
- Browser spricht nie direkt mit dem Dienst; `POSTER_TOKEN` verlässt den Server nie.
- Ohne `POSTER_URL` ist Posten aus: Knopf zeigt „Handy-Dienst in den Settings eintragen".
- Threads-Haken nur aktiv, wenn Instagram gewählt; Startwert an; letzte Wahl wird gemerkt (pro Browser, `localStorage`, in try/catch).
- KI-Hashtag-Fehler blockieren nie: dann nur feste Tags.
- UI-Texte Deutsch. Vorlage-Platzhalter: `{title}`, `{twitch}`, `{hashtags}`. Standardvorlage: `{title}\n\nLive: {twitch}\n\n{hashtags}`.
- Ränder fürs Verlängern: 20 s je Seite, geklemmt auf `[0, VOD-Dauer]`.
- Neue Pipeline-Renders: `clip.layout`, Facecam aus `web._load_facecam` → sonst `config.yaml clip.facecam`, `game=None`, Untertitel nach `clip.burn_subtitles`.
- Alle Dateinamen-Parameter mit `web._SAFE_NAME` prüfen und auf `data/<vod>/clips/` begrenzen (wie `clip_to_tiktok`).

## Review Focus

1. Dienst nicht erreichbar / Timeout → Seite lädt trotzdem, Statuszeile „Handy-Dienst offline", Senden zeigt Fehlertext statt 500. Test in Task 2 + 4.
2. Clip-Datei verschwindet oder wird während des Sendens neu gerendert → Senden liest die Datei einmal komplett, 404 wenn weg. Test in Task 4.
3. Doppelklick auf „Senden" → nur ein Post (Knopf sperrt clientseitig; serverseitig Lock pro Clip). Test in Task 4.
4. `run_at` in der Vergangenheit → wird als „sofort" behandelt (Dienst macht das schon); UI zeigt Uhrzeit lokal. Test in Task 4.
5. Clip ohne Eintrag in `moments.json` und ohne `_edit_` im Namen (alte/umbenannte Datei) → behandelt wie Editor-Clip (nur kürzen). Test in Task 1.

---

## File Structure

| Datei | Verantwortung |
|---|---|
| `src/posting.py` (neu) | Text bauen, Hashtags mischen, Limits prüfen, `posts.json` lesen/schreiben, Clip-Art + Schnittfenster bestimmen, KI-Hashtags |
| `src/poster_client.py` (neu) | httpx-Aufrufe an den Dienst, Fehler → `PosterError` |
| `src/posting_web.py` (neu) | `APIRouter` mit allen `/api/post/...`-Endpunkten |
| `src/clip.py` | + `trim_clip()`, + `probe_duration()` |
| `src/settings_store.py` | + 5 Einträge |
| `src/web.py` | Router einbinden, `_FREE_KEEP`, Settings-Textarea, `src_available`/`posting` an Template |
| `src/templates/clips_vod.html` | Statuszeile, Post-Feld, Zeitleiste, JS |
| `src/templates/index.html` | Textarea für `POST_TEMPLATE` |
| `tests/conftest.py`, `tests/test_*.py` (neu) | Tests |
| `requirements-dev.txt` (neu) | `pytest>=8` |
| `CLAUDE.md` | Abschnitt Posting + „Tests: `pytest`" |

Test-Befehl überall: `.venv/Scripts/python -m pytest tests -q` (lokal Windows), im Container `python -m pytest tests -q`.

---

### Task 1: Reine Posting-Logik + Testgerüst

**Files:**
- Create: `src/posting.py`, `tests/conftest.py`, `tests/test_posting.py`, `requirements-dev.txt`

**Interfaces:**
- Produces:
  - `MARGIN_S = 20.0`; `LIMITS = {"instagram": {"chars": 2200, "hashtags": 30}, "tiktok": {"chars": 4000, "hashtags": None}}`; `DEFAULT_TEMPLATE = "{title}\n\nLive: {twitch}\n\n{hashtags}"`
  - `normalize_tags(tags: list[str]) -> list[str]` — `#` voran, Leerzeichen/Satzzeichen außer `_` raus, leere weg
  - `merge_hashtags(fixed: list[str], ai: list[str]) -> list[str]` — feste zuerst, Duplikate (case-insensitive) raus
  - `parse_fixed_tags(raw: str) -> list[str]` — trennt an Leerzeichen/Komma/Zeilenumbruch, dann `normalize_tags`
  - `build_caption(template: str, title: str, twitch: str, hashtags: list[str]) -> str` — `\n` als Literal in der Vorlage = Zeilenumbruch; leerer `twitch` entfernt die ganze Zeile, die `{twitch}` enthält; Ergebnis `.strip()`, Mehrfach-Leerzeilen → eine
  - `check_caption(caption: str) -> dict` → `{"chars": int, "hashtags": int, "links": int, "ok": {"instagram": bool, "tiktok": bool}, "errors": {platform: str}}`; `hashtags = caption.count("#")`; Links per Regex `(?:https?://|www\.)\S+|\b[\w-]+\.(?:tv|com|de|gg|net|org|io)(?:/\S*)?`
  - `clip_kind(name: str, moments: dict[int, dict]) -> str` — `"pipeline"` wenn führende Zahl in `moments`, sonst `"edit"`
  - `PostStore(vod_dir: Path)` mit `get(clip: str) -> dict`, `update(clip: str, **fields) -> dict`, `add_post(clip: str, rec: dict) -> None`; Datei `posts.json` = `{clip_name: {"trim": {"mode": "source"|"clip", "start": float, "end": float} | None, "caption": str|None, "ai_tags": list[str]|None, "posts": [ {"id": int, "platforms": [...], "dry_run": bool, "run_at": float, "created": float, "status": str} ]}}`; atomar schreiben (`.tmp` + `os.replace`), `threading.Lock` auf Modulebene
  - `ACTIVE = {"queued", "running"}`; `is_locked(entry: dict) -> bool`

- [ ] **Step 1: `requirements-dev.txt` = `pytest>=8.0`; `pip install -r requirements-dev.txt` in `.venv`.** `tests/conftest.py` setzt vor jedem Import `TG_DATA_DIR` auf `tmp_path_factory`-Ordner, `USERS=t:t`, `SESSION_SECRET=x`, und fügt Repo-Root zu `sys.path`.

- [ ] **Step 2: Failing tests in `tests/test_posting.py`**

```python
def test_merge_dedupes_case_insensitive_fixed_first():
    assert merge_hashtags(["#StarCitizen", "gaming"], ["#starcitizen", "#fail"]) == ["#StarCitizen", "#gaming", "#fail"]

def test_build_caption_drops_twitch_line_when_empty():
    c = build_caption(DEFAULT_TEMPLATE, "Boom", "", ["#a"])
    assert c == "Boom\n\n#a"

def test_build_caption_literal_backslash_n():
    assert build_caption("{title}\\n{hashtags}", "T", "x", ["#a"]) == "T\n#a"

def test_check_caption_instagram_hashtag_limit():
    r = check_caption(" ".join(f"#t{i}" for i in range(31)))
    assert r["hashtags"] == 31 and r["ok"] == {"instagram": False, "tiktok": True}

def test_check_caption_counts_links():
    assert check_caption("Live: twitch.tv/x und https://a.b/c")["links"] == 2

def test_clip_kind_unknown_name_is_edit():          # Review Focus 5
    assert clip_kind("123_funny_x.mp4", {}) == "edit"
    assert clip_kind("123_funny_x.mp4", {123: {}}) == "pipeline"
    assert clip_kind("old.mp4", {}) == "edit"

def test_store_roundtrip_and_lock(tmp_path):
    s = PostStore(tmp_path)
    s.add_post("c.mp4", {"id": 7, "status": "queued", "platforms": ["tiktok"]})
    assert is_locked(s.get("c.mp4")) is True
    s.update("c.mp4", posts=[{"id": 7, "status": "done"}])
    assert is_locked(PostStore(tmp_path).get("c.mp4")) is False
```

- [ ] **Step 3: Run → FAIL (ImportError).** `.venv/Scripts/python -m pytest tests/test_posting.py -q`
- [ ] **Step 4: Implement `src/posting.py` per Interfaces.**
- [ ] **Step 5: Run → PASS.**
- [ ] **Step 6: Commit** `feat(posting): caption, hashtag and posts.json logic` (+ `requirements-dev.txt`, tests).

---

### Task 2: Client für den Handy-Dienst

**Files:**
- Create: `src/poster_client.py`, `tests/test_poster_client.py`

**Interfaces:**
- Produces:
  - `class PosterError(Exception)` — `.status: int | None`, Nachricht = `detail` aus der Antwort oder Netzwerkfehler-Text
  - `configured() -> bool` — `POSTER_URL` gesetzt
  - `status() -> dict`, `create(video: Path, meta: dict) -> int`, `get(pid: int) -> dict`, `edit(pid: int, **fields) -> dict`, `cancel(pid: int) -> dict`, `retry(pid: int) -> dict`, `shot(pid: int, platform: str) -> bytes`
  - Modulvariable `_transport: httpx.BaseTransport | None = None` (Tests setzen `httpx.MockTransport`)
- Liest `POSTER_URL` (ohne Slash am Ende) und `POSTER_TOKEN` bei jedem Aufruf aus `os.environ`; Header `Authorization: Bearer <token>`; Timeout 10 s, `create` 300 s (Upload). `meta` als JSON-String im Formfeld `meta`, Datei im Feld `video` mit `filename=video.name`, `content-type video/mp4`.

- [ ] **Step 1: Failing tests**

```python
def test_create_sends_bearer_and_meta(tmp_path, monkeypatch):
    seen = {}
    def handler(req):
        seen["auth"] = req.headers["authorization"]; seen["body"] = req.read()
        return httpx.Response(200, json={"id": 5})
    monkeypatch.setenv("POSTER_URL", "http://p:3300/"); monkeypatch.setenv("POSTER_TOKEN", "tok")
    monkeypatch.setattr(pc, "_transport", httpx.MockTransport(handler))
    f = tmp_path / "a.mp4"; f.write_bytes(b"xx")
    assert pc.create(f, {"platforms": ["tiktok"], "caption": "c"}) == 5
    assert seen["auth"] == "Bearer tok" and b'"platforms"' in seen["body"]

def test_http_error_detail_becomes_postererror(monkeypatch):
    monkeypatch.setattr(pc, "_transport", httpx.MockTransport(lambda r: httpx.Response(409, json={"detail": "nur geplante Posts sind änderbar"})))
    with pytest.raises(pc.PosterError, match="nur geplante") as e: pc.edit(1, caption="x")
    assert e.value.status == 409

def test_unreachable_is_postererror(monkeypatch):          # Review Focus 1
    def boom(r): raise httpx.ConnectError("refused")
    monkeypatch.setattr(pc, "_transport", httpx.MockTransport(boom))
    with pytest.raises(pc.PosterError): pc.status()
```

- [ ] **Step 2: Run → FAIL.** `.venv/Scripts/python -m pytest tests/test_poster_client.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(posting): HTTP client for the phone poster`.

---

### Task 3: Rendern — Neu-Schnitt aus Quelle, Kürzen ohne Quelle, Zurücksetzen

**Files:**
- Modify: `src/clip.py` (+ `probe_duration`, `trim_clip`), `src/posting.py` (+ Fenster/Render-Orchestrierung)
- Test: `tests/test_posting_render.py`

**Interfaces:**
- Consumes: `PostStore`, `clip_kind`, `MARGIN_S` (Task 1); `clip.render_custom` (bestehend)
- Produces:
  - `clip.probe_duration(path: Path) -> float` — ffprobe `format=duration`
  - `clip.trim_clip(src: Path, out: Path, start: float, end: float) -> Path` — Re-Encode wie `render_custom` (libx264 veryfast crf 20, aac 128k, faststart), über `.mp4.part` + `os.replace`; `src` darf == `out` sein (dann erst in `.part` rendern)
  - `posting.window(vod_dir: Path, clip: str, moments: dict[int, dict], cfg: dict) -> dict` → `{"kind", "mode": "source"|"clip", "start", "end", "min", "max", "orig_start", "orig_end", "source": bool}`.
    - `mode="source"` wenn `kind=="pipeline"` und `<vod>.mp4` existiert: Original = `moment.start - clip.pad_start` (≥ 0) bis `moment.end + clip.pad_end`; aktuell = gespeichertes `trim` (falls `mode=="source"`) sonst Original; `min/max` = aktuell ∓ `MARGIN_S`, geklemmt auf `[0, info.json duration]` (fehlt die Dauer → nur `≥ 0`).
    - sonst `mode="clip"`: Zeiten relativ zur Datei, `start=0`, `end=probe_duration`, `min=0`, `max=end`.
  - `posting.apply_trim(vod_dir: Path, clip: str, start: float, end: float, moments, cfg, facecam: dict | None, segments) -> dict` — wirft `LockedError` wenn `is_locked`; `ValueError` wenn `end - start < 1.0` oder außerhalb `[min, max]`. `source`: `render_custom(src, clips/clip, start=, duration=end-start, width/height/layout aus cfg["clip"], facecam=, game=None, segments=, burn_subtitles=cfg["clip"].get("burn_subtitles", False), title=moment["title"])`, speichert `trim`. `clip`: vor dem ersten Kürzen Kopie nach `clips/.orig/<clip>` (nur wenn noch keine da), dann `trim_clip(Datei, Datei, start, end)`, `trim` bleibt `None` (Datei ist jetzt der neue Stand). Gibt neues `window()` zurück.
  - `posting.reset_trim(...)` gleiche Parameter ohne start/end — `source`: rendert Original-Fenster, `trim=None`; `clip`: `.orig`-Datei zurückkopieren und löschen; ohne `.orig` → `ValueError("nichts zurückzusetzen")`. Sperre wie oben.
  - `class LockedError(Exception)`

- [ ] **Step 1: Failing tests** (ffmpeg-Aufrufe per `monkeypatch` auf `clip.render_custom`/`clip.trim_clip`/`clip.probe_duration` ersetzen, die Argumente aufzeichnen)

```python
def test_window_source_margins_clamped(vod):   # vod: fixture mit moments {100: start 100 end 130}, <vod>.mp4, info duration 140
    w = window(vod, "100_funny_x.mp4", moments, {"clip": {"pad_start": 1, "pad_end": 1}})
    assert (w["mode"], w["start"], w["end"], w["min"], w["max"]) == ("source", 99, 131, 79, 140)

def test_window_without_source_is_clip_mode(vod_no_src, fake_probe):   # probe → 32.0
    w = window(vod_no_src, "100_funny_x.mp4", moments, cfg)
    assert (w["mode"], w["start"], w["end"], w["max"]) == ("clip", 0, 32.0, 32.0)

def test_apply_trim_source_calls_render_custom_and_stores(vod, rec):
    apply_trim(vod, "100_funny_x.mp4", 95, 125, moments, cfg, facecam={"x":0,"y":0,"w":.2,"h":.2}, segments=None)
    assert rec.render_custom["start"] == 95 and rec.render_custom["duration"] == 30 and rec.render_custom["game"] is None
    assert PostStore(vod).get("100_funny_x.mp4")["trim"] == {"mode": "source", "start": 95, "end": 125}

def test_apply_trim_locked(vod):
    PostStore(vod).add_post("100_funny_x.mp4", {"id": 1, "status": "queued"})
    with pytest.raises(LockedError): apply_trim(vod, "100_funny_x.mp4", 95, 125, moments, cfg, None, None)

def test_clip_mode_backup_once_and_reset(vod_no_src, rec):
    apply_trim(vod_no_src, "100_funny_x.mp4", 2, 20, ...); apply_trim(vod_no_src, "100_funny_x.mp4", 1, 10, ...)
    orig = vod_no_src / "clips/.orig/100_funny_x.mp4"
    assert orig.read_bytes() == b"ORIGINAL"          # zweites Kürzen überschreibt Sicherung nicht
    reset_trim(vod_no_src, "100_funny_x.mp4", ...)
    assert (vod_no_src / "clips/100_funny_x.mp4").read_bytes() == b"ORIGINAL" and not orig.exists()

def test_too_short_rejected(vod):
    with pytest.raises(ValueError): apply_trim(vod, "100_funny_x.mp4", 100, 100.5, ...)
```

- [ ] **Step 2: Run → FAIL.** `.venv/Scripts/python -m pytest tests/test_posting_render.py -q`
- [ ] **Step 3: Implement `probe_duration`, `trim_clip` in `src/clip.py`; `window`, `apply_trim`, `reset_trim`, `LockedError` in `src/posting.py`.**
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(posting): re-cut clips from source or trim in place`.

---

### Task 4: Endpunkte (`src/posting_web.py`) + Einbindung + Settings + Free

**Files:**
- Create: `src/posting_web.py`, `tests/test_posting_web.py`
- Modify: `src/web.py` (Router einbinden nach `app = FastAPI(...)`; `_FREE_KEEP` += `posts.json, transcript.json, audio.json, chat.json, facecam.json`), `src/settings_store.py`, `src/templates/index.html`

**Interfaces:**
- Consumes: Task 1–3; aus `web`: `_SAFE_NAME`, `_moments_map`, `_load_facecam`, `data_dir`, `load_config`, `current_user` — der Router importiert sie **innerhalb** der Handler (`from . import web`), um Zirkelimport zu vermeiden; Pfadprüfung über eine Hilfsfunktion `_clip_path(vod, clip) -> Path` (404 sonst).
- `SETTING_KEYS` += `("POSTER_URL", "Handy-Dienst URL (z. B. http://192.168.178.x:3300)", False)`, `("POSTER_TOKEN", "Handy-Dienst Token", True)`, `("POST_TWITCH_URL", "Twitch-Link für Posts", False)`, `("POST_HASHTAGS", "Feste Hashtags", False)`, `("POST_TEMPLATE", "Post-Textvorlage ({title} {twitch} {hashtags})", False)`. `index.html`: für `POST_TEMPLATE` und `POST_HASHTAGS` `<textarea rows=3>` statt `<input>`; echte Zeilenumbrüche aus der Textarea speichern (`build_caption` versteht beides).
- `posting.suggest_hashtags(title: str, category: str, transcript_excerpt: str, cfg: dict, meter) -> list[str]` (in `src/posting.py`, hier gebaut weil erst hier gebraucht): `llm.structured(role="text", system=<deutsch: „Gib 3–5 passende, gängige TikTok/Instagram-Hashtags für diesen Gaming-Clip zurück, ohne die festen">, content=[TextPart(...)], schema={"type":"object","properties":{"hashtags":{"type":"array","items":{"type":"string"},"maxItems":5}},"required":["hashtags"]}, tool_name="hashtags", cfg=cfg, meter=meter, max_tokens=300, model=(cfg.get("refine") or {}).get("model"))` → `normalize_tags`. Transkript-Ausschnitt = Segmente aus `transcript.json`, die das Clip-Fenster schneiden, max. 1500 Zeichen. Kosten: `CostMeter` mit vorhandenem `cost.json` mergen nicht nötig — nur `meter.usd` ins Log. Jede Exception → `[]`.

Endpunkte (alle hinter Login, Middleware deckt `/api/*` mit 401 ab):

| Methode/Pfad | Verhalten |
|---|---|
| `GET /api/poster/status` | `{"configured": bool, "online", "battery", "busy", "queued", "next_run", "error"?}`; Dienst-Fehler → `200 {"configured": true, "online": false, "error": msg}` |
| `GET /api/post/{vod}/{clip}` | `{"window": window(...), "caption": entry.caption or build_caption(...), "ai_tags", "fixed_tags", "check": check_caption(caption), "posts": [...], "locked": bool, "configured": bool}`; aktualisiert dabei Status aller Posts mit `status in ACTIVE` per `poster_client.get` (Fehler ignorieren) und speichert sie. Erzeugt KI-Tags beim ersten Aufruf (`ai_tags is None`) und speichert sie (auch `[]`). |
| `GET /api/post/{vod}/{clip}/signals?start=&end=` | `{"audio": [[t,s],...], "chat": [...], "cuts": [segment-Grenzen]}` aus `audio.json`/`chat.json`/`transcript.json` im Bereich; im `clip`-Modus Zeiten relativ zur Datei (VOD-Zeit − `max(0, moment.start − clip.pad_start)`, nur bei ungekürzten Pipeline-Clips; Editor-Clips und bereits in-place gekürzte Clips → leere Listen) |
| `POST /api/post/{vod}/{clip}/hashtags` | KI-Tags neu, speichert, liefert neuen Text-Vorschlag |
| `PUT /api/post/{vod}/{clip}/draft` body `{caption}` | speichert Entwurf, liefert `check` |
| `POST /api/post/{vod}/{clip}/trim` body `{start, end}` | `apply_trim`; `LockedError` → 409 `"erst Post abbrechen"`; `ValueError` → 400 |
| `POST /api/post/{vod}/{clip}/reset` | `reset_trim`, gleiche Fehler |
| `POST /api/post/{vod}/{clip}/send` body `{caption, platforms, threads, dry_run, run_at}` | Lock pro `(vod, clip)` (`threading.Lock` in dict, nicht-blockierend → 409 „läuft schon"); `check_caption` gegen gewählte Plattformen → 400 mit Fehlertext; Datei fehlt → 404; `poster_client.create(path, {"platforms", "caption", "clip_name": clip, "title", "source": f"{vod}/{clip}", "run_at", "threads", "dry_run"})`; Erfolg → `add_post`, Entwurf speichern; `PosterError` → 502 mit Text |
| `PATCH /api/post/{vod}/{clip}/posts/{pid}` body `{caption?, run_at?}` | `poster_client.edit`, Eintrag aktualisieren |
| `POST .../posts/{pid}/cancel`, `.../retry` | durchreichen, Status speichern |
| `GET /api/post/shot/{pid}/{platform}` | PNG durchreichen (`platform in {"tiktok","instagram"}`) |

- [ ] **Step 1: Failing tests** (`TestClient(web.app)`, eingeloggt über `POST /login` mit `t:t`; `poster_client`-Funktionen und `posting.suggest_hashtags` per `monkeypatch` ersetzt; VOD-Ordner mit `moments.json`, Clip-Datei, ohne Quelle)

```python
def test_panel_builds_caption_from_settings(client, vod, monkeypatch):
    monkeypatch.setenv("POST_HASHTAGS", "#starcitizen gaming"); monkeypatch.setenv("POST_TWITCH_URL", "twitch.tv/x")
    monkeypatch.setattr(posting, "suggest_hashtags", lambda *a, **k: ["#fail"])
    j = client.get(f"/api/post/{vod.id}/100_funny_x.mp4").json()
    assert j["caption"] == "Boom\n\nLive: twitch.tv/x\n\n#starcitizen #gaming #fail"

def test_status_offline_is_200(client, monkeypatch):                         # Review Focus 1
    monkeypatch.setenv("POSTER_URL", "http://p")
    monkeypatch.setattr(poster_client, "status", raise_(poster_client.PosterError("refused")))
    assert client.get("/api/poster/status").json()["online"] is False

def test_send_rejects_over_limit_for_instagram(client, vod):
    r = client.post(f"/api/post/{vod.id}/100_funny_x.mp4/send", json={"caption": "#a " * 31, "platforms": ["instagram"]})
    assert r.status_code == 400 and "Instagram" in r.text

def test_send_records_post_and_locks_trim(client, vod, monkeypatch):
    monkeypatch.setattr(poster_client, "create", lambda p, m: 9)
    client.post(f"/api/post/{vod.id}/100_funny_x.mp4/send", json={"caption": "x", "platforms": ["tiktok"], "run_at": 0})
    assert client.post(f"/api/post/{vod.id}/100_funny_x.mp4/trim", json={"start": 1, "end": 10}).status_code == 409

def test_send_missing_file_404(client, vod):                                 # Review Focus 2
    (vod.path / "clips/100_funny_x.mp4").unlink()
    assert client.post(f"/api/post/{vod.id}/100_funny_x.mp4/send", json={"caption": "x", "platforms": ["tiktok"]}).status_code == 404

def test_send_concurrent_second_is_409(client, vod, monkeypatch):            # Review Focus 3
    # create blockiert über threading.Event; zweiter Request im Thread → 409

def test_path_traversal_rejected(client, vod):
    assert client.get(f"/api/post/{vod.id}/..%2Finfo.json").status_code in (400, 404)

def test_free_keeps_posting_files(client, vod):
    for n in ("posts.json", "transcript.json", "audio.json", "chat.json", "facecam.json"): (vod.path / n).write_text("[]")
    client.post(f"/vods/{vod.id}/free")
    assert all((vod.path / n).exists() for n in ("posts.json", "transcript.json", "audio.json", "chat.json", "facecam.json"))
```

- [ ] **Step 2: Run → FAIL.** `.venv/Scripts/python -m pytest tests/test_posting_web.py -q`
- [ ] **Step 3: Implement Router, `suggest_hashtags`, Settings-Keys, Textareas, `_FREE_KEEP`, `app.include_router(posting_web.router)`.**
- [ ] **Step 4: Run → PASS; dann ganze Suite** `.venv/Scripts/python -m pytest tests -q` → alles grün.
- [ ] **Step 5: Commit** `feat(posting): API routes, settings and free-keep for phone posting`.

---

### Task 5: UI auf der Clip-Seite

**Files:**
- Modify: `src/templates/clips_vod.html`, `src/web.py` (`clips_vod` übergibt `"posting": poster_client.configured()`)

**Interfaces:**
- Consumes: Endpunkte aus Task 4 (Antwortformen dort).

Inhalt (Optik passend zur Seite, Layout wie Entwurf `trim.html` aus dem Brainstorming):
- **Statuszeile** im Header: `📱 Handy: online · 82 % · 2 geplant · nächster 18:00` bzw. `offline`/`nicht eingerichtet → Settings`. Pollt `/api/poster/status` alle 15 s.
- Pro Clip (auch im JS-Pfad, der Clips dynamisch nachrendert, Zeile ~86) Knopf **„📱 Posten"**, klappt Feld auf, lädt `GET /api/post/...`.
- **Zeitleiste**: Balken `min..max`; Bereich außerhalb des aktuellen Clips grau; zwei Griffe (Pointer-Events, Maus + Touch) für In/Out, Abspielkopf; Anzeige `In h:mm:ss,d · Länge m:ss · Out …`; darunter Signal-Leiste (Audio + Chat als Balken) aus `/signals`; Haken „an Sätzen einrasten" (Standard an) snappt Griffe auf `cuts` innerhalb 1,5 s. Mindestlänge 1 s.
- **Sofort-Vorschau**: `<video>` mit `source`-Modus → `{base}/source/{vod}` (bzw. `/proxy/{vod}`-Datei, wenn vorhanden), sonst Clip-Datei; spielt `start→end` in Schleife (`timeupdate` → zurück auf `start`).
- **„🎬 Fertigen Clip rendern"** → `POST /trim`; Spinner; danach Clip-`<video>` mit Cache-Buster neu laden. **„Zurücksetzen"** → `POST /reset`. Beide ausgegraut + Hinweis „erst Post abbrechen", wenn `locked`. Ungerenderte Griff-Änderung → Senden-Knopf zeigt „erst rendern" und ist gesperrt (Anti-Ziel 1).
- **Text**: Textarea mit Vorschlag; Zähler live client-seitig (gleiche Regeln wie `check_caption`; Zahlen rot bei Überschreitung); „KI-Tags neu" → `POST /hashtags`; Entwurf speichern bei `blur` → `PUT /draft`.
- **Plattform**: ☐ TikTok ☐ Instagram ☐ Threads (disabled ohne Instagram; Startwert/letzte Wahl aus `localStorage` in try/catch) ☐ Probelauf.
- **Zeit**: ◉ sofort ○ geplant `<input type=datetime-local>` → `run_at = Date.parse(...)/1000`.
- **„📱 An Handy senden"**: sperrt sich sofort beim Klick; Fehlertext aus Antwort anzeigen.
- **Post-Liste** unter dem Feld: jüngster Post groß, ältere klein; Status-Badge (geplant/läuft/gepostet/teilweise/fehlgeschlagen/abgebrochen), Ergebnis je Plattform, Screenshot-Thumbnails (`/api/post/shot/...`), Knöpfe Abbrechen (queued), Wiederholen (failed/partial), Uhrzeit/Text ändern (queued → PATCH). Pollt `GET /api/post/...` alle 5 s nur solange ein Post aktiv ist.
- „Free MB"-Bestätigung: Zusatz „Danach können Clips nur noch gekürzt, nicht mehr verlängert werden." wenn mindestens ein Clip nicht gepostet ist (`clips_vod` übergibt `unposted: bool` aus `PostStore`).

- [ ] **Step 1: Implementieren.**
- [ ] **Step 2: Lokal prüfen:** `TG_DATA_DIR=<Testordner mit echtem VOD-Ordner von der Box (scp data/<vod>/ ohne Quelle)> .venv/Scripts/python -m src.main web`, Browser `http://localhost:9443/clips/<vod>`: Feld klappt auf, Zeitleiste zieht, Zähler reagieren, Threads deaktiviert ohne Instagram, Statuszeile „nicht eingerichtet". Ohne lokales ffmpeg kein Render-Test hier — das passiert in Task 6.
- [ ] **Step 3: `pytest tests -q` → grün.**
- [ ] **Step 4: Commit** `feat(posting): post panel with timeline trim, preview and phone status`.

---

### Task 6: Doku, Deploy auf die ClipBox, Ende-zu-Ende-Probe

**Files:**
- Modify: `CLAUDE.md` (Abschnitt „Posting über das Handy" unter Upload: Ablauf, `posts.json`, Router-Datei, Sperre; bei „No test suite" → `pytest tests -q`, `requirements-dev.txt`), `deploy/local/README.md` (Settings-Einträge `POSTER_URL`/`POSTER_TOKEN`)

- [ ] **Step 1: Doku anpassen, committen** `docs: phone posting`.
- [ ] **Step 2: Push** `git push origin clipbox`.
- [ ] **Step 3: Box aktualisieren:** `ssh -i ~/.ssh/clipbox_ed25519 clip@192.168.178.61 'cd /opt/twitch-generator && git pull --ff-only && cd deploy/local && docker compose -f docker-compose.local.yml up -d --build app'` → Erwartet: `app` neu gestartet; `curl -s localhost:9443/health` auf der Box → ok. Vorher prüfen, dass kein Job läuft (`/api/jobs`), sonst warten.
- [ ] **Step 4: Settings im UI eintragen** (`POSTER_URL`, `POSTER_TOKEN` — Token vom Nutzer bzw. aus `.env` auf dem Phone-Host).
- [ ] **Step 5: Probe auf der Box:** Clip-Seite eines VODs mit Quelle: Griff nach links ziehen, rendern, Vorschau zeigt früheren Start; Senden mit **Probelauf** an TikTok → Status läuft → gepostet (Probelauf), Screenshot erscheint. Echten Post macht der Nutzer selbst.
