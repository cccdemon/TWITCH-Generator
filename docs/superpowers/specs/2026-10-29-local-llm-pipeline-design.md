# Design: TWITCH-Generator lokal auf Heim-PC mit lokalem LLM

Datum: 2026-10-29
Status: Entwurf, wartet auf Freigabe

## 1. Absicht

Die Pipeline (Twitch-VOD → Highlight-Erkennung → vertikale Clips) soll auf einem
PC im Haus laufen statt auf dem gemieteten Server, und die LLM-Arbeit (Transkript
scannen, Frames bewerten, finale Auswahl, Facecam-Erkennung) soll ein **lokales
Modell** machen statt Claude. Grund: Claude-API-Kosten pro VOD. Claude bleibt
Coding-Werkzeug für die Entwicklung, verschwindet aber aus dem Laufzeitpfad.

Der PC bekommt dafür ein **frisches Linux**, installiert von einem **USB-Stick**,
das nach der Installation ohne weitere Handarbeit die komplette Anwendung
hochfährt.

## 2. Ziele / Nicht-Ziele / Anti-Ziele

**Ziele**
- Null Claude-Tokens pro VOD-Durchlauf im Standardbetrieb.
- Gleiche Pipeline, gleiche Web-UI, gleiche Caches (`data/<vod_id>/…`). Nur die
  Modell-Aufrufe und Whisper wechseln das Backend.
- Whisper auf GPU, damit ein größeres Modell (bessere Transkripte → bessere
  Humor-/Timing-Urteile) in akzeptabler Zeit läuft.
- Bootbarer USB-Stick: Stick rein, booten, einmal bestätigen, warten, Web-UI
  erreichbar.
- Umschaltbar: ein Config-Wert `llm.provider` wählt `ollama` (Standard lokal)
  oder `anthropic` (heutiges Verhalten). Prod-Config bleibt unverändert.

**Nicht-Ziele**
- Kein lokaler Coding-/Ops-Agent (OpenCode, Aider o.ä.) auf dem PC.
- Kein Live-System, das dauerhaft vom USB-Stick läuft. Der Stick ist Installer.
- Windows wird nicht angefasst: kein Shrink der Windows-Platte, kein Mount
  von NTFS, keine Migration von Windows-Daten.
- Keine Migration vom Prod-Server (läuft parallel weiter): kein Restore von
  `data/`, `settings.json`, `oauth.json`.
- Keine Änderung an Upload, Editor, Auth, Job-Queue.

**Anti-Ziele (das wäre ein Fehlschlag, auch wenn es "läuft")**
- Pipeline läuft, aber Vision/Refine fallen leise auf CPU zurück und ein VOD
  dauert 10 Stunden. GPU-Nutzung muss sichtbar geprüft werden (Log-Zeile).
- Pipeline läuft, aber Claude wird stillschweigend doch noch aufgerufen (z.B.
  Facecam-Erkennung vergessen). Jeder Claude-Aufruf muss durch `src/llm.py`
  gehen; `ANTHROPIC_API_KEY` darf im lokalen Setup fehlen.
- Prod-Deployment (`suite.raumdock.org/vod`) bricht, weil Defaults umgestellt
  wurden.

## 3. Hardware & Randbedingungen

| | |
|---|---|
| CPU | 6 Kerne |
| RAM | 32 GB |
| GPU | AMD Radeon RX 9070 XT, 16 GB VRAM, RDNA4, ROCm-Name `gfx1201` |
| Platten | 2 × 256 GB. Platte 1 = Windows 11 (bleibt), Platte 2 = heutige Datenplatte → wird Ubuntu |
| Boot | Dual-Boot Pflicht. Kein BitLocker auf Platte 1. |
| Netz | Kabel-Ethernet, DHCP. UI nur im LAN (kein Caddy, kein TLS) |
| Zeitbudget | 6 h pro Nacht für einen VOD-Durchlauf |
| Prod | `suite.raumdock.org/vod` läuft parallel weiter, unabhängig |

Konsequenzen:
- **Platte 2 wird komplett gelöscht.** Daten dort vorher wegsichern — steht
  fett im USB-README. Beide Platten sind gleich groß, Autoinstall kann sie
  nicht sicher unterscheiden → Plattenwahl bleibt **interaktiv** (ein Klick:
  Platte 2, „use entire disk"). Alles andere läuft automatisch.
- Dual-Boot: Ubuntu bekommt eigene EFI-Partition auf Platte 2, GRUB mit
  `os-prober` aktiviert (Ubuntu 24.04 hat ihn per Default aus) → Bootmenü
  zeigt Ubuntu + Windows, Default Ubuntu, Timeout 10 s. Windows-Platte wird
  nie gemountet.
- Platzbudget auf 256 GB: OS ≈ 10 GB, Modelle ≈ 17 GB, Docker-Images ≈ 5 GB
  → ≈ 220 GB für `data/`. Reicht für 5–10 VODs mit Caches. UI-Funktion
  „Speicher freigeben" (behält Clips/moments/cost/info) existiert schon;
  `bootstrap.sh` setzt Docker-Log-Rotation (`json-file`, 10 MB × 3).
- 6-h-Budget: Vision-Triage seriell auf dem 8B-VLM ist der GPU-Zeitfresser
  (6-h-VOD, 10-s-Grid ≈ 2160 Frames ≈ 135 Calls). Lokale Config setzt
  `vision.interval_seconds: 15` (≈ 1440 Frames) als Start; Spike misst
  Sekunden pro Triage-Call und der Builder rechnet daraus hoch. Wenn ein
  typischer VOD nicht in ~4 h passt (Puffer), Interval auf 20 s oder
  `triage_model` kleiner (`qwen3-vl:4b`). Whisper large-v3-turbo auf GPU
  ist kein Engpass (Faktor ≫ Echtzeit).
- RDNA4 braucht Kernel ≥ 6.13 im `amdgpu`-Treiber. Ubuntu 24.04 LTS mit
  HWE-Kernel (24.04.3+ → 6.14) erfüllt das. ROCm-Userspace kommt **im
  Ollama-Container** mit, kein `amdgpu-install` auf dem Host.
- 16 GB VRAM: Text- und Vision-Modell passen nicht gleichzeitig mit Reserve.
  Ollama lädt/entlädt selbst (`OLLAMA_MAX_LOADED_MODELS=1`). Pipeline-Reihenfolge
  hilft: Whisper ∥ Vision laufen zuerst parallel (Vision 8B ≈ 6 GB + Whisper
  ≈ 1,6 GB), danach Text-Modell für analyze + refine.
- 6 Kerne: ffmpeg-Decode, Frame-Extraktion, Rendering bleiben CPU.
  `whisper.cpu_threads` wird im GPU-Pfad irrelevant.

## 4. Was gebaut wird — Überblick

```
┌─────────────────────────── Heim-PC (Ubuntu 24.04 Server, Docker) ────────────────────────────┐
│                                                                                              │
│  app  (twitch-generator, wie heute, CPU)                                                     │
│   ├─ web :9443 ─────────────────────────────────────────── LAN-Browser                       │
│   └─ pipeline-subprocess                                                                     │
│        ├─ transcribe.py ── HTTP ──► whisper  (whisper.cpp, Vulkan, /dev/dri)                 │
│        │                                      ggml-large-v3-turbo                            │
│        ├─ vision.py  ──┐                                                                     │
│        ├─ analyze.py ──┤── src/llm.py ── HTTP ──► ollama  (ROCm, /dev/kfd + /dev/dri)        │
│        └─ refine.py  ──┘        │                     ├─ qwen3-vl:8b  (Vision, Facecam)      │
│                                 └─ provider=anthropic ├─ qwen3:14b    (Text: analyze, refine)│
│                                    (optional, wie heute)                                     │
│  ollama-init (einmalig: pullt beide Modelle, beendet sich)                                   │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

Vier Arbeitspakete:

1. **`src/llm.py`** — Provider-Abstraktion für strukturierte Modell-Aufrufe
   (Anthropic Tool-Use ↔ Ollama `format`=JSON-Schema). `analyze`, `refine`,
   `vision` (Triage, Confirm, Facecam) rufen nur noch das.
2. **`src/transcribe.py`** — zweites Backend `whispercpp-server` neben
   `faster-whisper`; Chunking bleibt.
3. **Deploy lokal** — `docker-compose.local.yml` (app + ollama + whisper +
   ollama-init), `deploy/local/config.yaml`, `deploy/local/.env.example`.
4. **USB-Installer** — Ventoy-Stick mit Ubuntu-Server-ISO + Autoinstall
   `user-data`, das nach der Installation `deploy/local/bootstrap.sh` ausführt.

## 5. Arbeitspaket 1: `src/llm.py`

### 5.1 Schnittstelle

Ein öffentlicher Aufruf, der überall den heutigen `client.messages.create(...,
tools=[T], tool_choice=T)`-Block ersetzt:

```python
def structured(
    *, role: str,                 # "text" | "vision" — wählt Modell aus cfg
    system: str,
    content: list[Part],          # Part = TextPart(text) | ImagePart(path)
    schema: dict,                 # das heutige tool["input_schema"]
    tool_name: str,               # für den Anthropic-Pfad (tool_choice)
    cfg: dict, meter=None, max_tokens: int = 2048,
) -> dict:                        # geparstes Objekt gemäß schema
```

- `Part` ist bewusst neutral (kein Anthropic-Block-Format), damit der Ollama-Pfad
  Bilder als `images: [base64…]` und Text als Prompt zusammenbaut, der
  Anthropic-Pfad daraus die heutigen `{"type":"image",…}`-Blöcke macht.
- Rückgabe ist **immer ein dict** (bei Fehler: Exception). Die heutigen
  Parser (`_parse`, `_tool_items`, `_apply`) bleiben, lesen nur statt
  `block.input` jetzt direkt das dict. Sie validieren bereits defensiv
  (clamp, index-check) — beim lokalen Modell noch wichtiger.
- Die bestehenden Tool-Definitionen (`_MOMENT_TOOL`, `_TOOL`, `_FRAME_TOOL`,
  `_BURST_TOOL`, `_FACECAM_TOOL`) bleiben als Quelle für `schema`
  (= `tool["input_schema"]`) und `tool_name`.

### 5.2 Provider `anthropic`

Exakt heutiges Verhalten: `Anthropic(api_key=env("ANTHROPIC_API_KEY",
required=True))`, Tool-Use mit `tool_choice`, `meter.add(model, msg.usage)`.
Modellnamen weiter aus `analyze.model`, `vision.triage_model`, `vision.model`,
`refine.model`. Kein Verhaltenswechsel für Prod.

### 5.3 Provider `ollama`

- HTTP `POST {llm.ollama.base_url}/api/chat` via `httpx` (schon Dependency).
  Kein weiteres SDK.
- `format` = `schema` (Ollama Structured Outputs, JSON-Schema-constrained).
  Ersetzt Tool-Calling vollständig; funktioniert auch mit Vision-Modellen.
- `think: false` (Qwen3-Thinking kostet Zeit, hilft bei schema-constrained
  Output nicht), `options: {temperature: 0, num_ctx: …}`.
- `num_ctx` pro Rolle aus Config. Der Refine-Prompt ist groß: 30 Kandidaten ×
  Kontext ≈ 20–30k Tokens → `num_ctx: 32768` für `text`, `8192` für `vision`.
  Zu kleiner `num_ctx` schneidet Ollama still ab (Anti-Ziel). `llm.py` schätzt
  Prompt-Tokens (len/3) und loggt Warnung, wenn > 0,9·num_ctx.
- Bilder: `message.images = [base64…]`. Index-Hinweise ("index 7:") bleiben
  als Text im Prompt, in derselben Reihenfolge wie die Bilder.
- Modellauswahl: `role="text"` → `llm.ollama.text_model`,
  `role="vision"` → `llm.ollama.vision_model`. Die Claude-Modellnamen in den
  Stage-Sections werden im Ollama-Pfad **ignoriert**, damit die vorhandene
  `config.yaml` unverändert bleibt und nur der `llm:`-Block dazukommt.
- Antwort: `message.content` ist JSON-String → `json.loads`. Bei
  `JSONDecodeError` ein Retry, dann Exception. Aufrufer degradieren wie heute:
  vision-Batch wird übersprungen, refine fällt auf fused ranking.
- `meter.add(model, usage)`: Ollama liefert `prompt_eval_count`/`eval_count`;
  `llm.py` übersetzt in ein Objekt mit `input_tokens`/`output_tokens`.
  `pricing._rate()` prüft auf Präfix `ollama/` → `(0.0, 0.0)`; `meter.add`
  wird mit `f"ollama/{model}"` aufgerufen. `cost.json` bleibt korrekt ($0,
  Token-Zahlen sichtbar), UI zeigt 0.00. `pricing.estimate_cost` bleibt
  wie es ist (lernt nach dem ersten lokalen Lauf 0 $/s).
- Concurrency: `vision.concurrency` wird im Ollama-Pfad auf 1 gedeckelt.
  Ollama serialisiert pro Modell ohnehin (`OLLAMA_NUM_PARALLEL=1` bei 16 GB),
  parallele Requests riskieren nur Timeouts. Timeout pro Request
  `llm.ollama.timeout_seconds` (Default 600).

### 5.4 Cache-Verträglichkeit

`vision_triage.json`/`vision.json` speichern den Modellnamen in ihren Params.
Im Ollama-Pfad wird dort der aufgelöste Ollama-Modellname gespeichert, also
invalidieren bestehende Claude-Caches sauber. `candidates.json`/`moments.json`
haben keine Modell-Params — unverändert.

### 5.5 Prompt-Anpassungen (bewusst klein)

System-Prompts bleiben, bekommen für den Ollama-Pfad zwei Zusätze, die bei
kleinen Modellen messbar helfen:
- "Return ONLY the JSON object." (redundant mit `format`, schadet nicht).
- Im Refine-Prompt ein Beispiel-Objekt mit einem Clip (Few-Shot).

Rubrik, Kategorien, Regeln bleiben identisch, damit Ergebnisse zwischen
Providern vergleichbar sind.

## 6. Arbeitspaket 2: Whisper über whisper.cpp

### 6.1 Entscheidung

| Option | Verworfen weil |
|---|---|
| faster-whisper (CTranslate2) auf GPU | kein ROCm-Backend |
| PyTorch-ROCm + openai-whisper im App-Container | +5 GB Image, ROCm-Userspace im App-Container, gfx1201-Wheel-Lotterie |
| whisper.cpp **im** App-Container (Binary bauen) | Vulkan-Treiber + Build-Toolchain im App-Image |
| **whisper.cpp als eigener Container `ghcr.io/ggml-org/whisper.cpp:main-vulkan`, `whisper-server`** | gewählt: offizielles Image, App-Container bleibt CPU-only, saubere Trennung |

Modell: `ggml-large-v3-turbo` (≈ 1,6 GB, deutlich schneller als large-v3,
Qualität nah dran). Fällt bei VRAM-Not auf `ggml-small` zurück — nur Config.

### 6.2 Änderungen `transcribe.py`

- Neue Parameter `backend: "faster-whisper" | "whispercpp-server"` und
  `server_url`. `main.py` reicht `whisper.backend`, `whisper.server_url` durch.
- Chunking (`_split_audio_chunks`, 30-min-WAVs, Offset-Addition) bleibt
  **unverändert**: hält Requests klein, erlaubt Fortschritt pro Chunk, und
  `extract.py` erzeugt die Chunks weiterhin im Single-Pass.
- Pro Chunk: `POST {server_url}/inference` multipart (`file`, `language`,
  `response_format=verbose_json`, `temperature=0`). Antwort-Segmente
  (`start`/`end` in Sekunden) + Offset → `TranscriptSegment`. whisper.cpp
  server hat kein `vad_filter` wie faster-whisper; Halluzinations-Filter
  bleibt dem Refine-Prompt überlassen ("transcript is noisy").
  **Im Spike verifizieren:** exaktes Feldformat von `verbose_json` in der
  aktuellen whisper.cpp-Version. Fallback, falls Server kein `verbose_json`
  liefert: `srt`-Format parsen, das der Server sicher kann.
- `transcript.json`-Format bleibt gleich → Caches kompatibel.
- Fehler: Server nicht erreichbar → Exception (Whisper ist Pflichtsignal,
  heute auch). Log-Zeile mit Backend + Modell am Start.

## 7. Arbeitspaket 3: Lokales Deployment

### 7.1 `docker-compose.local.yml`

```yaml
services:
  app:
    build: .
    image: twitch-generator:latest
    restart: unless-stopped
    env_file: .env
    ports: ["9443:9443"]
    volumes:
      - ./data:/data
      - ./deploy/local/config.yaml:/app/config.yaml:ro
    depends_on: [ollama, whisper]

  ollama:
    image: ollama/ollama:rocm
    restart: unless-stopped
    devices: ["/dev/kfd", "/dev/dri"]
    group_add: ["${RENDER_GID}", "${VIDEO_GID}"]   # bootstrap.sh schreibt GIDs in .env
    environment:
      OLLAMA_MAX_LOADED_MODELS: "1"
      OLLAMA_NUM_PARALLEL: "1"
      OLLAMA_KEEP_ALIVE: "10m"
    volumes: ["ollama:/root/.ollama"]
    expose: ["11434"]

  ollama-init:                           # einmalig: Modelle ziehen
    image: ollama/ollama:rocm
    depends_on: [ollama]
    entrypoint: ["/bin/sh", "-c"]
    command: ["ollama pull qwen3-vl:8b && ollama pull qwen3:14b"]
    environment: { OLLAMA_HOST: "http://ollama:11434" }
    restart: "no"

  whisper:
    image: ghcr.io/ggml-org/whisper.cpp:main-vulkan
    restart: unless-stopped
    devices: ["/dev/dri"]
    group_add: ["${RENDER_GID}", "${VIDEO_GID}"]
    volumes:
      - whisper-models:/models
      - ./deploy/local/whisper-entrypoint.sh:/entrypoint.sh:ro
    entrypoint: ["/entrypoint.sh"]        # lädt Modell falls fehlt, startet whisper-server
    environment: { WHISPER_MODEL: "large-v3-turbo", WHISPER_LANG: "de" }
    expose: ["8080"]

volumes: { ollama: {}, whisper-models: {} }
```

Der Builder entscheidet: exakter Pfad des Server-Binaries im Image,
Healthchecks, Startreihenfolge.

`jobs.py` bleibt: `nice -n 19`, `cwd="/app"` passen im Container.

### 7.2 `deploy/local/config.yaml`

Kopie von `config.yaml` mit diesen Abweichungen:

```yaml
llm:
  provider: ollama
  ollama:
    base_url: http://ollama:11434
    text_model: qwen3:14b
    vision_model: qwen3-vl:8b
    num_ctx: { text: 32768, vision: 8192 }
    timeout_seconds: 600
whisper:
  backend: whispercpp-server
  server_url: http://whisper:8080
  model: large-v3-turbo        # informativ/Log; Datei kommt aus whisper-Service
  language: de
  chunk_seconds: 1800
vision:
  concurrency: 1
  interval_seconds: 15        # 6-h-Budget; Spike kalibriert (10 = heutiger Prod-Wert)
```

Die Haupt-`config.yaml` bekommt den `llm:`-Block mit `provider: anthropic`
und `whisper.backend: faster-whisper`, damit Prod identisch weiterläuft und
die Defaults dokumentiert sind.

### 7.3 Secrets lokal

`.env` braucht nur `USERS`, `SESSION_SECRET`, `PUBLIC_URL=http://<pc-ip>:9443`,
`RENDER_GID`, `VIDEO_GID`, optional `TWITCH_*`. `ANTHROPIC_API_KEY` entfällt.
`settings_store.py` behält das Feld (für Provider-Umschaltung); die UI zeigt
bei `provider: ollama` einen Hinweis "nicht benötigt" (eine Zeile im Template).

### 7.4 Sichtbarkeit (gegen Anti-Ziel 1)

- `main.py` loggt am Pipeline-Start: Provider, Text-/Vision-Modell,
  Whisper-Backend.
- `llm.py` loggt beim ersten Ollama-Call `eval_count`/`eval_duration` → tok/s
  im Job-Log. CPU-Fallback ist 10–20× langsamer und sofort erkennbar.
- `bootstrap.sh` prüft nach dem Start `docker exec ollama rocminfo | grep
  gfx1201` und bricht mit klarer Meldung ab, wenn nichts gefunden.

## 8. Arbeitspaket 4: USB-Installer

### 8.1 Entscheidung

| Option | Verworfen weil |
|---|---|
| Eigenes ISO remastern (cubic, xorriso) | Build auf Windows umständlich, bei jedem Ubuntu-Pointrelease neu |
| Rufus + Stock-ISO, danach Script von Hand | funktioniert, aber "bootbar + fertig" nicht erfüllt |
| **Ventoy-Stick + Stock Ubuntu-Server-ISO + Ventoy-Autoinstall-Plugin mit `user-data`** | gewählt: Stick wird unter Windows mit `Ventoy2Disk.exe` gebaut; ISO + `user-data` sind normale Dateien auf dem Stick; Update = Datei ersetzen |

### 8.2 Inhalt `deploy/local/usb/`

- `README.md` — Schritte: Ventoy2Disk.exe, ISO (ubuntu-24.04.x-live-server-amd64)
  kopieren, `ventoy/ventoy.json` + `autoinstall/user-data` kopieren, booten,
  Autoinstall bestätigen, warten, Browser auf `http://<ip>:9443`, Passwort aus
  Konsole (`motd`).
- `ventoy.json` — Auto-Install-Plugin-Eintrag, der das Ubuntu-ISO mit
  `user-data` verknüpft.
- `autoinstall/user-data` (cloud-init, `autoinstall: version: 1`):
  - locale/keyboard de, Hostname `clipbox`, User `clip` mit Passwort-Hash
    (Platzhalter; README sagt, wie erzeugen), SSH an.
  - Storage: `interactive-sections: [storage]` — Installer fragt die Platte.
    README-Anweisung: Platte 2 wählen, „Use an entire disk", kein LVM, keine
    Verschlüsselung. **Löscht Platte 2 komplett.** Alles andere automatisch.
  - `kernel: package: linux-generic-hwe-24.04` (RDNA4).
  - Packages: `docker.io`, `docker-compose-v2`, `git`, `curl`,
    `mesa-vulkan-drivers`, `vulkan-tools` (Host-Diagnose), `linux-firmware`,
    `os-prober`.
  - `late-commands`: `GRUB_DISABLE_OS_PROBER=false`, `GRUB_TIMEOUT=10`,
    `update-grub` (Windows im Bootmenü); User in `docker`, `render`, `video`;
    Repo nach `/opt/twitch-generator` klonen (URL/Branch als Platzhalter oben
    in der Datei); systemd-Unit `clipbox-bootstrap.service` (oneshot, erster
    Boot) → `deploy/local/bootstrap.sh`.
- README-Hinweise vor dem Start: Daten von Platte 2 sichern; Secure Boot:
  Ventoy verlangt bei aktivem Secure Boot einmalig eine MOK-Bestätigung
  (Schritte im README) oder Secure Boot im BIOS aus; Windows „Schnellstart"
  ist egal, weil NTFS nie gemountet wird.
- `bootstrap.sh` (idempotent, auf dem installierten System):
  1. `.env` aus `.env.example` erzeugen falls fehlt: `SESSION_SECRET` random,
     `USERS=clip:<random>`; Passwort nach `/opt/twitch-generator/FIRST-LOGIN.txt`
     und in `/etc/motd`.
  2. GIDs von `render`/`video` ermitteln → `RENDER_GID`/`VIDEO_GID` in `.env`.
  3. `PUBLIC_URL` aus erster Nicht-Loopback-IP.
  4. `docker compose -f docker-compose.local.yml up -d --build`.
  5. Warten bis `ollama-init` fertig; `rocminfo`-Check (gfx1201);
     Whisper-Server antwortet.
  6. Ergebnis + URL in `motd`. Unit deaktiviert sich nach Erfolg.

### 8.3 Netzwerkannahme

DHCP im Heimnetz, UI unter `http://<ip>:9443` ohne TLS. OAuth-Redirects für
Upload-Plattformen funktionieren damit nur, wenn `PUBLIC_URL` von außen
erreichbar ist — Upload ist Nicht-Ziel, bleibt aber konfigurierbar.

## 9. Reihenfolge & Spike

Bevor AP1–2 gebaut werden, ein **Wegwerf-Spike auf dem Ziel-PC** (1–2 h, mit
einem beliebigen Ubuntu-Live-Stick + Docker):

1. `ollama/ollama:rocm` starten, `rocminfo` → `gfx1201`?
2. `qwen3-vl:8b` pullen, ein Grid-Frame mit dem Triage-Schema per `format`
   schicken → valides JSON? tok/s?
3. `whisper.cpp:main-vulkan` starten, 30-min-WAV posten → Laufzeit,
   `verbose_json`-Format notieren.

Ergebnis entscheidet: Modellgrößen final (8B/14B vs. 32B-quant), `num_ctx`,
ob `verbose_json` oder `srt` geparst wird. Spike-Code wird nicht übernommen.

Bau-Reihenfolge danach: AP1 (`llm.py`, Provider-Switch, Prod bleibt grün) →
AP2 (`transcribe.py`) → AP3 (compose + config) → AP4 (USB). Jedes AP einzeln
testbar; AP1+2 auch gegen Ollama/whisper.cpp auf dem Dev-Rechner (CPU reicht
zum Funktionstest).

## 10. Qualitätsprüfung

Kein Testsuite im Repo. Abnahme wie heute: ein VOD durch beide Provider (zwei
`TG_DATA_DIR`, weil `candidates.json` providerabhängig ist). Vergleich
`moments.json`: Überlappung der Top-5-Fenster, Stichprobe der Clips.
Erwartung (ehrlich): lokal urteilt über Humor schlechter; Chat- und Audio-
Signale tragen die Fusion. Wenn Refine lokal zu viel Müll durchlässt:
`refine.enabled: false` als Rückfall gibt es heute schon.

## 11. Entscheidungen des Auftraggebers (aus dem Gespräch)

- Pipeline-LLM komplett lokal, Claude nur noch Coding-Werkzeug.
- Zielhardware: 6 Kerne / 32 GB / RX 9070 XT, 2 × 256 GB.
- Dual-Boot mit bestehendem Windows 11 (Platte 1) ist Pflicht; Ubuntu auf
  Platte 2. Kein BitLocker.
- Kabel-Netz. Zeitbudget 6 h pro Nacht und VOD.
- Prod-Server läuft parallel weiter, keine Migration.
- USB-Stick bootbar.

## 12. Entscheidungen von Claude, zum Gegenlesen

- Ubuntu Server 24.04 LTS + HWE-Kernel statt 25.x (LTS, Docker-Pakete stabil).
- Plattenwahl im Installer interaktiv (ein Klick), weil beide Platten 256 GB
  sind; Rest Autoinstall. GRUB mit os-prober, Default Ubuntu, 10 s.
- Alles in Docker, ROCm-Userspace nur im Ollama-Image, kein `amdgpu-install`.
- Ollama Structured Outputs (`format`) statt Tool-Calling.
- Modelle: `qwen3:14b` (Text), `qwen3-vl:8b` (Vision), `ggml-large-v3-turbo`
  (Whisper). Alles Config, nichts fest verdrahtet.
- whisper.cpp als eigener Container über HTTP.
- USB = Ventoy + Autoinstall; Platte 2 wird komplett überschrieben.
- Provider-Switch bleibt im Code, Prod-Default `anthropic`.
- `vision.concurrency` im Ollama-Pfad auf 1; lokal `vision.interval_seconds: 15`
  als Startwert fürs 6-h-Budget, Spike kalibriert.
- Docker-Log-Rotation in `bootstrap.sh` (256-GB-Platte).

## 13. Dem Builder überlassen

- Exakte Formulierung des Few-Shot im Refine-Prompt.
- Healthchecks/Startreihenfolge in compose, Modell-Download im
  whisper-entrypoint.
- Ob `Part` dataclass oder TypedDict.
- Retry-Details (Anzahl, Backoff) in `llm.py`.
- Darstellung des "ANTHROPIC_API_KEY nicht benötigt"-Hinweises in der UI.
