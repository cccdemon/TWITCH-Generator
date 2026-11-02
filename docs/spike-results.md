# Spike-Ergebnisse (Ziel-PC: RX 9070 XT, gfx1201)

Datum: _ausfüllen_
Live-System: Ubuntu 24.04.x Desktop Live, Kernel `uname -r` = _…_
Kommandos: `docs/spike/README.md`

## 1. Ollama ROCm

| Feld | Wert |
|---|---|
| Image `ollama/ollama:rocm` Id / Created | |
| `ollama --version` | |
| Log-Zeile `inference compute …` (vollständig) | |
| `library=` (rocm / cpu / vulkan) | |
| `compute=` (erwartet gfx1201) | |
| VRAM `total=` / `available=` | |

Fazit: GPU erkannt? ☐ ja ☐ nein → Maßnahme: _…_

## 2. Vision `qwen3-vl:8b` (Triage-Schema)

| Lauf | Frames/Call | Wandzeit s | load s | prompt_tokens | prefill tok/s | gen_tokens | gen tok/s | JSON ok | alle Indizes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | | | | | | | | |
| 2 | 1 | | | | | | | | |
| 3 | 1 | | | | | | | | |
| 1 | 16 | | | | | | | | |
| 2 | 16 | | | | | | | | |
| 3 | 16 | | | | | | | | |

`ollama ps` während Lauf: SIZE = _…_, PROCESSOR = _…_

Optional `qwen3-vl:4b` (16 Frames): Wandzeit _…_ s, gen tok/s _…_

**T16** (Sekunden pro 16-Frame-Call, warm) = _…_

## 3. Text `qwen3:14b` (25k-Prompt)

| Variante | num_ctx | est. tokens | prompt_eval_count | Verhältnis | prefill tok/s | gen tok/s | Wandzeit s | JSON ok | `ollama ps` SIZE / PROCESSOR |
|---|---|---|---|---|---|---|---|---|---|
| a) FA + q8 KV | 32768 | | | | | | | | |
| b) Kontrolle | 8192 | | | | | | | | |
| c) qwen3:8b (nur falls a) nicht 100 % GPU) | 32768 | | | | | | | | |

Log-Signatur beim Abschneiden (aus b): `_…_`

Fazit: Modell für `text` = _…_, `num_ctx` = _…_, Ollama-Env: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0` ☐ nötig ☐ nicht nötig

## 4. whisper.cpp Vulkan

| Feld | Wert |
|---|---|
| Image Id / Created | |
| Mesa im Image (`dpkg -l mesa-vulkan-drivers`) | |
| Log `ggml_vulkan: Found … devices` | |
| `-fa` lief? | |
| 30-min-WAV: Wandzeit s / Faktor | |
| `verbose_json` Top-Level-Keys | |
| Segment-Keys | |
| `start`/`end` Typ (float Sekunden erwartet) | |
| `srt` Lauf: Wandzeit s | |

Fazit: Parser = ☐ `verbose_json` (`segments[].start/end`) ☐ `srt`

## 5. Hochrechnung 6-h-VOD (Agent füllt aus)

| Posten | Formel | Zeit |
|---|---|---|
| Download 6 h VOD (~10–15 GB) | Leitung | |
| Extract single-pass | CPU, ≈ 0.1× Echtzeit | |
| Whisper 12 Chunks | 12 × 1800 / Faktor | |
| Vision Triage @15 s | 90 × T16 | |
| Vision Confirm | ≈ 40 × T12 | |
| analyze 36 Chunks | 36 × T_analyze | |
| refine | 1 Call | |
| Render 10 Clips | CPU | |
| **Summe** | | **< 4 h?** |

Entscheidung `vision.interval_seconds`: _15 / 20_; Vision-Modell: _8b / 4b_.
