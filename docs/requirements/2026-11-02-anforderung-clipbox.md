# Anforderung: ClipBox — TWITCH-Generator als lokale Appliance mit USB-Installer

Datum: 2026-11-02
Auftraggeber: Torsten (Betreiber von TWITCH-Generator)
Empfänger: ein LLM-Coding-Agent, der dieses Vorhaben als eigenes Projekt aufzieht
Grundlage: Design-Entwurf `docs/superpowers/specs/2026-10-29-local-llm-pipeline-design.md` im Repo TWITCH-Generator (dort alle Hintergründe; dieses Dokument ist die verbindliche Anforderung)

---

## 0. In einem Satz

Baue aus dem bestehenden Projekt TWITCH-Generator (Twitch-VOD → Highlight-Clips, Python 3.11, Docker) eine Appliance namens **ClipBox**, die auf einem Heim-PC mit AMD-GPU vollständig ohne Cloud-LLM läuft, und liefere dafür einen **bootbaren USB-Installer**, der nach dem Booten ohne weitere Handarbeit (außer Plattenwahl und einer Bestätigung) ein fertiges System mit erreichbarer Web-UI hinterlässt.

## 1. Ausgangslage

TWITCH-Generator existiert und läuft produktiv auf einem gemieteten Server (`suite.raumdock.org/vod`). Pipeline:

```
Twitch-VOD download → ffmpeg single-pass (Audio-Chunks, Frames, Loudness)
→ Whisper (faster-whisper, CPU)                                → transcript.json
→ Chat-Velocity / Audio-Bursts / Vision (Claude Haiku+Sonnet)  → Signale
→ Text-Kandidaten (Claude) → Fusion → Refine (Claude, ein globaler Call)
→ Render (ffmpeg, vertikal split facecam/game) → Clips, optional Upload
```

Alle LLM-Arbeit geht heute an die Anthropic-API (Tool-Use für strukturierte Ausgabe). Kosten pro VOD sind der Grund für dieses Projekt. Der Prod-Server bleibt unverändert in Betrieb; es findet **keine Migration** statt.

Das Repo enthält eine `CLAUDE.md` mit Architekturbeschreibung. **Lies sie zuerst.** Relevante Fakten, die du sonst aus mehreren Dateien zusammensuchen müsstest:

| Thema | Fakt |
|---|---|
| Claude-Aufrufstellen | `src/analyze.py` (Text-Kandidaten), `src/refine.py` (globaler Re-Score), `src/vision.py` (Triage mit Haiku, Confirm mit Sonnet, Facecam-Erkennung). Alle nutzen `client.messages.create(..., tools=[T], tool_choice=T)` und parsen `block.input`. Tool-Definitionen heißen `_MOMENT_TOOL`, `_TOOL`, `_FRAME_TOOL`, `_BURST_TOOL`, `_FACECAM_TOOL`. |
| Kostenzähler | Jeder Call füttert `pricing.CostMeter.add(model, usage)`; Preise in `pricing._PRICES`; Summe landet in `data/<vod>/cost.json`. Parallele Calls locken um `meter.add`. |
| Whisper | `src/transcribe.py`, faster-whisper, 30-min-Chunks aus `extract.py` (`audio_chunks/*.wav`), Offsets werden addiert. Chunking muss bleiben. |
| Caches | Jede Stufe schreibt JSON nach `data/<vod_id>/`, speichert ihre Parameter mit, baut bei Abweichung neu. `vision_triage.json`/`vision.json` speichern den Modellnamen. |
| Config | `config.yaml` als dict via `load_config()`; neuere Stufen (`chat`, `audio`, `vision`, `score`, `refine`) lesen ihre eigene Section. Secrets über `config.env()` aus Env; `settings_store.py` schreibt UI-Werte nach `data/settings.json`. |
| Web/Jobs | FastAPI `:9443`, Login über `USERS`-Env, Session über `SESSION_SECRET`. Jeder Job ist ein Subprocess `nice -n 19 python -m src.main run --vod …` mit `cwd="/app"`. |
| Verifikation | Kein Testsuite, kein Linter. Abnahme = Pipeline auf einem VOD laufen lassen, `data/<vod>/candidates.json` gegen `moments.json` lesen. |

## 2. Zielhardware & Umgebung (fix, nicht verhandelbar)

| | |
|---|---|
| CPU | 6 Kerne |
| RAM | 32 GB |
| GPU | AMD Radeon RX 9070 XT, 16 GB VRAM, RDNA4, ROCm-Target `gfx1201` |
| Platten | 2 × 256 GB SSD. **Platte 1 = Windows 11, bleibt unangetastet. Platte 2 wird komplett gelöscht und zu Ubuntu.** Beide gleich groß → Installer kann sie nicht sicher automatisch unterscheiden. |
| Boot | UEFI. Dual-Boot Pflicht: GRUB-Menü mit Ubuntu (Default, 10 s Timeout) und Windows. Kein BitLocker auf Platte 1. Secure Boot ggf. aktiv (siehe Risiken). |
| Netz | Kabel-Ethernet, DHCP. UI nur im LAN, HTTP ohne TLS, kein Reverse-Proxy. |
| Zeitbudget | Ein 6-Stunden-VOD muss in **< 6 h Wandzeit** durchlaufen, Ziel 4 h. |
| Bedienrechner | Windows 11. Der USB-Stick wird unter Windows erstellt. |
| Zugriff des Agenten | Du arbeitest auf einem Dev-Rechner ohne die Ziel-GPU. Alles, was die GPU braucht, läuft über definierte Übergabepunkte durch den Menschen (Abschnitt 8). |

## 3. Ziele

1. **Null Cloud-Tokens im Standardbetrieb.** Kein Aufruf an `api.anthropic.com` während eines VOD-Durchlaufs. `ANTHROPIC_API_KEY` darf im lokalen Setup fehlen.
2. **Gleiche Pipeline, gleiche UI, gleiche Cache-Formate.** Nur die Backends für LLM und Whisper wechseln.
3. **Whisper auf GPU** mit `large-v3-turbo` (bessere Transkripte als heute auf CPU).
4. **Umschaltbar:** `llm.provider: ollama | anthropic` und `whisper.backend: faster-whisper | whispercpp-server`. Prod-Config und Prod-Verhalten bleiben **byte-identisch** im Default.
5. **Bootbarer USB-Installer:** Stick rein → booten → Platte 2 wählen → bestätigen → warten → `http://<ip>:9443` ist erreichbar, Erstlogin steht auf der Konsole.

## 4. Nicht-Ziele

- Kein lokaler Coding-Agent auf dem PC.
- Kein Live-System vom Stick. Der Stick ist Installer, nicht Laufzeit.
- Windows wird nicht angefasst: kein Partition-Shrink, kein NTFS-Mount, keine Datenmigration.
- Keine Migration von `data/`, `settings.json`, `oauth.json` vom Prod-Server.
- Keine Änderung an Upload, Editor, Auth, Job-Queue (außer dem einen UI-Hinweis in 6.3).
- Kein Caddy, kein TLS, keine Erreichbarkeit von außen.

## 5. Anti-Ziele (gilt als Fehlschlag, auch wenn "es läuft")

| Anti-Ziel | Pflichtmaßnahme |
|---|---|
| Modelle laufen still auf CPU, VOD dauert 10 h | GPU-Nutzung wird beim Start geprüft (`rocminfo` → `gfx1201`) und im Job-Log pro Lauf sichtbar gemacht (tok/s, Whisper-Faktor). |
| Claude wird doch noch irgendwo aufgerufen (z. B. Facecam-Erkennung vergessen) | **Jeder** Modellaufruf geht durch `src/llm.py`. Abnahmetest läuft ohne `ANTHROPIC_API_KEY`. |
| Prod bricht durch geänderte Defaults | Haupt-`config.yaml` bekommt nur dokumentierte Defaults (`provider: anthropic`, `backend: faster-whisper`). Prod-Smoke-Test ist Teil der Abnahme. |
| `num_ctx` zu klein, Ollama schneidet den Refine-Prompt still ab | Token-Schätzung im Code, Warnung bei > 0,9·`num_ctx`. |
| Platte 1 (Windows) wird überschrieben | Plattenwahl bleibt interaktiv. README warnt fett. Kein `match: size` o. ä. im Autoinstall. |

## 6. Lieferumfang

Alles landet in **einem Git-Repository** (Fork oder Branch von TWITCH-Generator; der USB-Installer klont genau dieses Repo). Änderungen am Anwendungscode müssen upstream-mergefähig bleiben (Prod-Default unverändert).

### 6.1 Anwendung: Provider-Abstraktion `src/llm.py`

- Eine Funktion `structured(role, system, content, schema, tool_name, cfg, meter, max_tokens) -> dict` ersetzt alle heutigen `messages.create`-Blöcke. `role ∈ {"text", "vision"}` wählt das Modell.
- `content` ist eine Liste neutraler Teile (`TextPart`, `ImagePart(path)`), kein Anthropic-Blockformat.
- Provider `anthropic`: exakt heutiges Verhalten (Tool-Use, `tool_choice`, Modellnamen aus den Stage-Sections, `meter.add`).
- Provider `ollama`: `POST {base_url}/api/chat` via `httpx`, `format` = JSON-Schema (Structured Outputs), `think: false`, `temperature: 0`, `num_ctx` pro Rolle aus Config, Bilder als Base64 in `message.images`. Antwort `json.loads`, ein Retry bei `JSONDecodeError`, dann Exception. Modelle aus `llm.ollama.text_model` / `vision_model`; die Claude-Modellnamen der Stages werden im Ollama-Pfad ignoriert.
- Kosten: `meter.add(f"ollama/{model}", usage)` mit `prompt_eval_count`/`eval_count` → `input_tokens`/`output_tokens`; `pricing` liefert für Präfix `ollama/` den Preis 0. `cost.json` bleibt gültig.
- `vision.concurrency` wird im Ollama-Pfad auf 1 gedeckelt. Request-Timeout aus `llm.ollama.timeout_seconds` (Default 600).
- Aufrufer (`analyze`, `refine`, `vision` inkl. Facecam) lesen das dict statt `block.input`; die vorhandenen defensiven Parser bleiben. Fehlerverhalten wie heute: Vision-Batch wird übersprungen, Refine fällt auf fused ranking.
- Prompts: System-Prompts bleiben; im Ollama-Pfad zusätzlich "Return ONLY the JSON object." und im Refine-Prompt ein Ein-Clip-Few-Shot. Rubrik und Kategorien unverändert.
- Log-Zeile beim Pipeline-Start: Provider, Text-/Vision-Modell, Whisper-Backend. Beim ersten Ollama-Call: tok/s.

### 6.2 Anwendung: Whisper-Backend `whispercpp-server`

- `transcribe.py` bekommt `backend` und `server_url`; `main.py` reicht `whisper.backend`, `whisper.server_url` durch.
- Pro 30-min-Chunk: `POST {server_url}/inference` (multipart: `file`, `language`, `response_format`, `temperature=0`). Segmente + Chunk-Offset → `TranscriptSegment`. Format von `transcript.json` unverändert.
- Antwortformat (`verbose_json` vs. `srt`) wird im Spike verifiziert, nicht geraten.
- Server nicht erreichbar → Exception (Whisper ist Pflichtsignal). Log: Backend + Modell.

### 6.3 Lokales Deployment `deploy/local/`

- `docker-compose.local.yml` mit Services `app` (CPU, wie heute), `ollama` (`ollama/ollama:rocm`, `/dev/kfd` + `/dev/dri`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`), `ollama-init` (einmalig `pull` beider Modelle), `whisper` (`ghcr.io/ggml-org/whisper.cpp:main-vulkan`, `whisper-server`, Modell wird im Entrypoint geladen falls fehlt). Healthchecks und Startreihenfolge sind deine Entscheidung, müssen aber so sein, dass `app` nicht vor `ollama`/`whisper` Jobs annimmt.
- `deploy/local/config.yaml`: Kopie der Haupt-Config mit `llm.provider: ollama`, `whisper.backend: whispercpp-server`, `vision.concurrency: 1`, `vision.interval_seconds: 15` (Startwert, Spike kalibriert).
- Startmodelle: `qwen3:14b` (Text), `qwen3-vl:8b` (Vision), `ggml-large-v3-turbo` (Whisper). Alles Config, nichts fest verdrahtet. Spike darf Größen ändern.
- `.env.example`: `USERS`, `SESSION_SECRET`, `PUBLIC_URL`, `RENDER_GID`, `VIDEO_GID`, optional `TWITCH_*`. Kein `ANTHROPIC_API_KEY`.
- UI: Bei `provider: ollama` zeigt die Settings-Seite beim Anthropic-Key den Hinweis "nicht benötigt". Mehr UI-Änderung nicht.
- Docker-Log-Rotation (`json-file`, 10 MB × 3) wird vom Bootstrap gesetzt.
- `bootstrap.sh` (idempotent, läuft als systemd-oneshot beim ersten Boot):
  1. `.env` aus Example erzeugen, falls fehlt: `SESSION_SECRET` random, `USERS=clip:<random>`; Passwort nach `FIRST-LOGIN.txt` und `/etc/motd`.
  2. GIDs von `render`/`video` ermitteln → `.env`.
  3. `PUBLIC_URL` aus erster Nicht-Loopback-IP.
  4. `docker compose -f docker-compose.local.yml up -d --build`.
  5. Warten bis `ollama-init` fertig, `rocminfo | grep gfx1201` im Ollama-Container, Whisper-Server antwortet. Bei Fehlschlag: klare Meldung in `motd`, Unit bleibt aktiv (erneuter Boot versucht es nochmal).
  6. Erfolg + URL in `motd`, Unit deaktiviert sich.

### 6.4 USB-Installer `deploy/local/usb/`

Gewählter Weg: **Ventoy-Stick + unverändertes Ubuntu-Server-ISO + Ventoy-Autoinstall-Plugin mit cloud-init `user-data`.** Kein ISO-Remaster.

- `README.md` (Deutsch, für den Menschen am Windows-PC): Ventoy2Disk.exe, ISO `ubuntu-24.04.x-live-server-amd64` (aktuellstes Point-Release, mind. 24.04.3 wegen HWE-Kernel ≥ 6.14 für RDNA4), Dateien kopieren, Passwort-Hash erzeugen (`openssl passwd -6` oder Windows-Alternative), booten, Platte 2 wählen („Use an entire disk", kein LVM, keine Verschlüsselung), bestätigen, warten, Browser, Erstlogin. Fett: **Daten auf Platte 2 vorher sichern.** Secure-Boot-Abschnitt: MOK-Enrollment für Ventoy oder Secure Boot im BIOS aus.
- `ventoy/ventoy.json`: Auto-Install-Plugin-Eintrag ISO ↔ `user-data`.
- `autoinstall/user-data` (`autoinstall: version: 1`):
  - Locale/Tastatur de, Hostname `clipbox`, User `clip` mit Passwort-Hash-Platzhalter, SSH an.
  - `interactive-sections: [storage]` — nur die Platte wird gefragt. Alles andere automatisch.
  - `kernel: package: linux-generic-hwe-24.04`.
  - Pakete: `docker.io`, `docker-compose-v2`, `git`, `curl`, `mesa-vulkan-drivers`, `vulkan-tools`, `linux-firmware`, `os-prober`.
  - `late-commands`: `GRUB_DISABLE_OS_PROBER=false`, `GRUB_TIMEOUT=10`, `update-grub`; User in `docker`, `render`, `video`; Repo nach `/opt/twitch-generator` klonen (URL + Branch als Platzhalter oben in der Datei); systemd-Unit `clipbox-bootstrap.service` anlegen und aktivieren.
- Optional, wenn ohne Aufwand: `build-usb.ps1`, das die Stick-Struktur aus dem Repo zusammenkopiert und den Hash in `user-data` einsetzt.

### 6.5 Dokumentation

- `deploy/local/README.md`: Architekturbild (app / ollama / whisper), Betrieb (Logs, Neustart, Modelle tauschen, Speicher freigeben), Rückfall auf `provider: anthropic`.
- `docs/spike-results.md`: Messwerte aus Abschnitt 7.
- Änderungen an `CLAUDE.md` des Repos: neue Module, neue Config-Keys, Gotchas.

## 7. Pflicht-Spike vor dem Bau (auf dem Ziel-PC, 1–2 h)

Mit beliebigem Ubuntu-Live-Stick + Docker, Ergebnisse in `docs/spike-results.md`. Du lieferst die exakten Kommandos, der Mensch führt sie aus und gibt Output zurück.

1. `ollama/ollama:rocm` starten → `rocminfo` zeigt `gfx1201`? Welche Image-Version/ROCm-Version?
2. `qwen3-vl:8b` pullen, ein Grid-Frame mit dem Triage-Schema via `format` senden → valides JSON? Sekunden pro Call? tok/s?
3. `qwen3:14b` pullen, einen ~25k-Token-Prompt mit `num_ctx: 32768` senden → passt in VRAM? tok/s? Wird abgeschnitten?
4. `whisper.cpp:main-vulkan` starten, 30-min-WAV posten → Laufzeit (Faktor zu Echtzeit), exaktes Antwortformat von `verbose_json` notieren.
5. Hochrechnung: 6-h-VOD bei `interval_seconds: 15` ≈ 1440 Frames → Triage-Zeit? Passt alles in 4 h? Sonst `interval_seconds: 20` oder `qwen3-vl:4b`.

Spike-Code wird nicht übernommen. Der Spike entscheidet Modellgrößen, `num_ctx`, Antwortformat-Parser.

## 8. Vorgehen & Übergabepunkte

| Phase | Inhalt | Testbar ohne GPU? | Übergabe an Mensch |
|---|---|---|---|
| 0 | Repo lesen, `CLAUDE.md`, Spec, Spike-Kommandos vorbereiten | ja | Spike ausführen, Output zurück |
| 1 | `src/llm.py` + Umbau `analyze`/`refine`/`vision`; Prod-Pfad identisch | ja (Ollama auf CPU reicht für Funktionstest) | — |
| 2 | `transcribe.py` Backend | ja (whisper.cpp CPU-Image) | — |
| 3 | `deploy/local/` compose, config, bootstrap | teilweise | Compose auf Ziel-PC starten, Logs zurück |
| 4 | USB-Installer | nein | Stick bauen, booten, Protokoll zurück |
| 5 | Abnahme nach Abschnitt 9 | nein | VOD-Lauf, `cost.json`, `run.json`, Clips sichten |

Jede Phase einzeln committen. Phase 1 darf erst beginnen, wenn Spike-Ergebnis 1–2 vorliegt (sonst baust du gegen Annahmen).

## 9. Abnahmekriterien (alle müssen erfüllt sein)

1. **Prod unverändert:** Repo mit unveränderter Haupt-`config.yaml` und gesetztem `ANTHROPIC_API_KEY` verarbeitet einen VOD wie vor dem Umbau (gleiche Anzahl Claude-Calls, gleiche Modelle in `cost.json`).
2. **Null Cloud:** Lokales Setup **ohne** `ANTHROPIC_API_KEY` verarbeitet denselben VOD vollständig bis zu gerenderten Clips. `cost.json` enthält nur `ollama/…`-Einträge mit 0 $. Kein Import-/Auth-Fehler im Log.
3. **GPU nachweisbar:** Bootstrap-Check `gfx1201` grün. Job-Log zeigt tok/s des ersten Ollama-Calls und Whisper-Faktor ≫ 1× Echtzeit. Referenzwerte aus dem Spike im Log-Kommentar.
4. **Zeitbudget:** 6-h-VOD in < 6 h Wandzeit, dokumentiert über `run.json`.
5. **USB-Installer:** Vom Einschalten bis zur erreichbaren UI nur: Ventoy-Menü → ISO, Plattenwahl, eine Autoinstall-Bestätigung. Danach kein Tastendruck. Windows startet weiterhin aus dem GRUB-Menü. Zweiter Boot ohne Stick: alle Container laufen, UI erreichbar.
6. **Idempotenz:** `bootstrap.sh` zweimal ausführen ändert nichts. Reboot bringt alle Services zurück.
7. **Qualität:** Vergleich `moments.json` Anthropic vs. Ollama auf demselben VOD (zwei `TG_DATA_DIR`): Top-5-Fenster-Überlappung dokumentiert. Erwartung ehrlich: lokal urteilt schlechter über Humor; Chat/Audio tragen. Wenn Refine lokal zu viel durchlässt, ist `refine.enabled: false` der dokumentierte Rückfall.
8. **Doku vollständig:** USB-README reicht einem technisch versierten Laien ohne Rückfragen.

## 10. Risiken, die du aktiv behandeln musst

| Risiko | Umgang |
|---|---|
| `ollama/ollama:rocm` unterstützt `gfx1201` in der gezogenen Version nicht | Spike 1. Fallback: Image-Tag pinnen, der es kann; `HSA_OVERRIDE_GFX_VERSION` ist für RDNA4 kein zuverlässiger Weg, nicht als Lösung annehmen. |
| Text- und Vision-Modell passen nicht gleichzeitig in 16 GB | `OLLAMA_MAX_LOADED_MODELS=1`; Pipeline-Reihenfolge (Whisper ∥ Vision zuerst, dann Text) beibehalten. |
| whisper.cpp-Server liefert kein `verbose_json` | `srt` parsen. Spike 4 entscheidet. |
| Autoinstall wählt falsche Platte | Niemals automatisieren. `interactive-sections: [storage]` ist Pflicht. |
| Secure Boot blockiert Ventoy | README: MOK-Enrollment-Schritte oder BIOS-Option. |
| Ubuntu-Point-Release ändert HWE-Kernel-Paketname | `user-data` dokumentiert die ISO-Version, auf die sie getestet wurde. |
| 256 GB voll nach wenigen VODs | Docker-Log-Rotation; UI-Funktion „Speicher freigeben" existiert; README erklärt sie. |

## 11. Entscheidungen, die feststehen (nicht neu diskutieren)

- Ubuntu Server 24.04 LTS + HWE-Kernel. Alles in Docker. ROCm-Userspace nur im Ollama-Image, kein `amdgpu-install` auf dem Host.
- Ollama Structured Outputs (`format`) statt Tool-Calling.
- whisper.cpp als eigener Container über HTTP, Vulkan-Image.
- Ventoy + Autoinstall; Platte 2 wird komplett überschrieben; Plattenwahl interaktiv.
- Provider-Switch bleibt im Code; Prod-Default `anthropic`.

## 12. Entscheidungen, die dir überlassen sind

- Fork vs. Branch; Repo-/Projektname (Arbeitstitel ClipBox, Hostname `clipbox`).
- Few-Shot-Formulierung im Refine-Prompt.
- Healthchecks, Startreihenfolge, Whisper-Entrypoint-Details.
- `Part` als dataclass oder TypedDict; Retry-Anzahl/Backoff in `llm.py`.
- Darstellung des „nicht benötigt"-Hinweises in der UI.
- Ob `build-usb.ps1` gebaut wird.

## 13. Rückfragen

Stelle Rückfragen gebündelt am Ende von Phase 0, nicht einzeln. Alles, was in Abschnitt 11 steht, ist entschieden. Alles, was in Abschnitt 12 steht, entscheidest du und dokumentierst es kurz im README.
