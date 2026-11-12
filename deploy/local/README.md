# ClipBox — TWITCH-Generator lokal (Ollama + whisper.cpp auf AMD-GPU)

Betriebsdoku für den Heim-PC (Ubuntu Server 24.04, RX 9070 XT). Installation
über den USB-Stick: siehe [usb/README.md](usb/README.md). Hintergrund und
Anforderung: `docs/requirements/2026-11-02-anforderung-clipbox.md`.

## Architektur

```
┌──────────────────────── clipbox (Ubuntu 24.04 Server, Docker) ────────────────────────┐
│                                                                                       │
│  app  (twitch-generator:local, CPU)                                                   │
│   ├─ web :9443  ──────────────────────────────────────────  Browser im LAN            │
│   └─ Job-Subprocess (python -m src.main run)                                          │
│        ├─ transcribe.py ── HTTP /inference ──► whisper   whisper.cpp, Vulkan, /dev/dri │
│        │                                                 ggml-large-v3-turbo           │
│        ├─ vision.py  ──┐                                                               │
│        ├─ analyze.py ──┤── src/llm.py ── HTTP /api/chat ──► ollama   ROCm, /dev/kfd    │
│        └─ refine.py  ──┘   provider: ollama                   ├─ qwen3-vl:8b-instruct (Vision) │
│                                                               └─ qwen3:14b    (Text)   │
│  ollama-init  (einmalig: ollama pull beider Modelle, beendet sich mit 0)               │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

Dateien in diesem Ordner:

| Datei | Zweck |
|---|---|
| `docker-compose.local.yml` | app, ollama, ollama-init, whisper; Healthchecks, Startreihenfolge |
| `config.yaml` | Pipeline-Config mit `llm.provider: ollama`, `whisper.backend: whispercpp-server` |
| `.env.example` → `.env` | Secrets + GIDs + Modellnamen; wird vom Bootstrap erzeugt |
| `bootstrap.sh` | idempotenter Erststart (siehe unten) |
| `clipbox-bootstrap.service` | systemd-oneshot, ruft `bootstrap.sh` beim ersten Boot |
| `whisper-entrypoint.sh` | lädt das ggml-Modell falls fehlt, startet `whisper-server` |
| `usb/` | Ventoy-Stick: `ventoy.json`, `autoinstall/user-data`, `build-usb.ps1`, README |

## Erststart (was `bootstrap.sh` tut)

1. `.env` aus `.env.example`, falls fehlt: `USERS=clip:<zufällig>`, `SESSION_SECRET` zufällig.
   Passwort → `/opt/twitch-generator/FIRST-LOGIN.txt` (nur root/clip lesbar) und `/etc/motd`.
2. GIDs von `render`/`video` → `RENDER_GID`/`VIDEO_GID`.
3. `PUBLIC_URL=http://<erste LAN-IP>:9443`.
4. Modellnamen aus `config.yaml` (`llm.ollama.text_model`/`vision_model`, `whisper.language`) → `.env`.
5. Docker-Log-Rotation (`/etc/docker/daemon.json`: json-file, 10 MB × 3), nur wenn noch keine `daemon.json` existiert.
6. `docker compose up -d --build`, warten bis `ollama-init` mit 0 endet (Modell-Download ≈ 15 GB, Timeout 120 min).
7. GPU-Nachweis: Ollama-Log muss `inference compute … gfx1201` enthalten (das ROCm-Image hat kein `rocminfo`);
   whisper-Log muss ein Vulkan-AMD-Gerät zeigen, nicht `llvmpipe`.
8. `GET /health` auf :9443 → `/etc/motd` mit URL und Status, Unit deaktiviert sich.

Bei Fehlschlag bleibt die Unit aktiv (nächster Boot versucht es erneut) und `/etc/motd` nennt den Grund.
Zweimal ausführen ändert nichts: `.env` wird nur ergänzt, Compose ist deklarativ.

```bash
sudo /opt/twitch-generator/deploy/local/bootstrap.sh     # manuell erneut ausführen
journalctl -u clipbox-bootstrap -b                         # Log des ersten Boots
```

## Betrieb

Alle Befehle aus `/opt/twitch-generator/deploy/local`:

```bash
alias dc='docker compose -f docker-compose.local.yml'
dc ps                              # Status aller Container
dc logs -f app                     # Pipeline-/Web-Log
dc logs ollama | grep -i "inference compute"   # GPU erkannt? (gfx1201, library=rocm)
dc logs whisper | grep -i vulkan               # Vulkan-Gerät (RADV GFX1201, nicht llvmpipe)
dc restart app                     # nur die App
dc down && dc up -d                # alles neu (Modelle bleiben in den Volumes)
dc exec ollama ollama ps           # geladenes Modell, VRAM, "100% GPU"?
```

Im Job-Log (Web-UI oder `dc logs app`) stehen pro Lauf:

- `backends: llm=ollama text=… vision=… · whisper=whispercpp-server …` — am Start.
- `llm: first ollama call … gen=… tok (NN tok/s)` — GPU-Referenz: qwen3:14b ≈ 25–40 tok/s. 2–4 tok/s = CPU-Fallback, dann Ollama-Log prüfen.
- `whisper: backend=whispercpp-server …` und `whisper: chunk k/n` — 30-min-Chunk sollte in 1–3 min durch sein (Faktor ≫ 10×).
- `llm: prompt ≈N tokens vs num_ctx …` — Warnung, wenn ein Prompt nahe am Kontextfenster liegt (Ollama schneidet still ab). Dann `llm.ollama.num_ctx.text` erhöhen oder `analyze.max_clips`/`score.candidate_factor` senken.

### Modelle tauschen

1. `config.yaml`: `llm.ollama.text_model` / `vision_model` ändern (z. B. `qwen3:8b`, `qwen3-vl:4b`).
2. `sudo ./bootstrap.sh` — schreibt die Namen nach `.env`, `ollama-init` zieht das neue Modell.
   Oder von Hand: `dc exec ollama ollama pull <modell>` und `dc up -d`.
3. Alte Modelle löschen: `dc exec ollama ollama rm <modell>`.

Whisper-Modell: `WHISPER_MODEL` in `.env` (z. B. `small`, `medium`, `large-v3-turbo`), dann `dc up -d whisper`.
Der Entrypoint lädt die Datei beim Start nach `/models` (Volume `whisper-models`).

### Speicher freigeben (256-GB-Platte)

- Web-UI → VOD → **„Speicher freigeben"** (`POST /vods/<id>/free`): löscht Quellvideo, Frames, WAV-Chunks; behält Clips, `moments.json`, `cost.json`, `info.json`. Pro 6-h-VOD ≈ 10–15 GB.
- „Löschen" entfernt den ganzen VOD-Ordner.
- `df -h /` und `du -sh /opt/twitch-generator/data/*` zeigen, wo der Platz ist. Docker-Logs sind rotiert (30 MB/Container).
- `docker system df` / `docker image prune` für alte Images nach `--build`.

### Zeitbudget

Ziel: 6-h-VOD in < 4 h. Stellhebel in `config.yaml`:

| Hebel | Wirkung |
|---|---|
| `vision.interval_seconds` 15 → 20 | ¼ weniger Triage-Calls (der GPU-Zeitfresser) |
| `llm.ollama.vision_model: qwen3-vl:4b` | ≈ 2× schnellere Triage, etwas gröber |
| `vision.burst: false` | kein Tier-2, Vision nur Triage |
| `refine.enabled: false` | kein globaler Refine-Call; fused Ranking + Snapping |

`run.json` im VOD-Ordner enthält Start/ETA/Dauer; `cost.json` zeigt Token-Zahlen (0 $ bei Ollama).

### Posten über das Handy

TikTok/Instagram laufen über den Handy-Dienst **clip-poster** (`poster/`, eigener Host mit per USB angeschlossenem Handy, Port 3300). Einmalig in der Settings-Seite eintragen:

- `Handy-Dienst URL` = `http://<phone-host>:3300`, `Handy-Dienst Token` = `POSTER_TOKEN` aus `/opt/clip-poster/.env` auf dem Phone-Host
- `Twitch-Link für Posts`, `Feste Hashtags`, optional `Post-Textvorlage` (`{title}`, `{twitch}`, `{hashtags}`)

Danach auf der Clip-Seite eines VODs pro Clip „📱 Posten“: schneiden, Vorschau, Text prüfen, senden. Erst mit **Probelauf** testen.

## Rückfall auf Anthropic

`config.yaml` → `llm.provider: anthropic`, `ANTHROPIC_API_KEY` in `.env` oder in der Settings-Seite eintragen, `dc up -d`.
Dann gelten wieder `analyze.model`, `vision.triage_model`, `vision.model`, `refine.model`. Whisper bleibt davon unberührt
(`whisper.backend` separat). Die Vision-Caches (`vision_triage.json`, `vision.json`) speichern den Modellnamen und werden beim
Providerwechsel neu gebaut; `transcript.json`, `chat.json`, `audio.json` bleiben gültig.

## Entscheidungen (Abschnitt 12 der Anforderung)

- **Branch statt Fork**: `clipbox` im Repo `cccdemon/TWITCH-Generator`; `main` bleibt Prod. Installer klont `-b clipbox`.
- **`Part` als dataclass** (`TextPart`, `ImagePart`), Retry in `llm.py`: genau ein Wiederholungsversuch bei HTTP-Fehler oder ungültigem JSON, kein Backoff (Ollama ist lokal; Timeout 600 s pro Request).
- **GPU-Check** über Ollamas eigene Discovery-Logzeile statt `rocminfo` (im Image nicht enthalten).
- **Healthchecks**: `ollama list` (Ollama), `curl` auf `:8080` (whisper, `start_period` 15 min wegen Modell-Download); `app` startet erst nach `ollama-init` = exit 0.
- **Few-Shot im Refine-Prompt** (nur Ollama): ein Beispiel-Clip-Objekt mit `index/start/end/score/category/title/reason`, plus Hinweis, dass ein leeres Array erlaubt ist.
- **UI-Hinweis**: Settings-Seite zeigt beim Anthropic-Key den Badge „nicht benötigt (provider: ollama)".
- **`build-usb.ps1`** gebaut (kopiert Stick-Struktur, setzt Passwort-Hash).
- Ollama läuft mit `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`, damit `qwen3:14b` mit 32k Kontext in 16 GB bleibt; Spike-Ergebnis entscheidet, ob das reicht (sonst `qwen3:8b`).
