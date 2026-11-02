# Spike: GPU-Messungen auf dem Ziel-PC (Phase 0)

Ziel: vor dem Bau von `src/llm.py` und `transcribe.py` fünf Fragen auf dem echten
PC (RX 9070 XT, `gfx1201`) beantworten. Dauer ≈ 1–2 h, der Großteil ist Download.
Ergebnisse kommen nach `docs/spike-results.md` (Vorlage liegt dort, Felder ausfüllen
oder einfach die Terminal-Ausgabe zurückgeben).

Der Spike-Code hier wird **nicht** in die Anwendung übernommen.

---

## 0. Vorbereitung (einmalig, ≈ 20 min)

### 0.1 Live-Stick

Ubuntu **24.04.4 Desktop** Live-ISO (oder neuer). Nur dieses Point-Release bringt
den 6.17-HWE-Kernel mit, den RDNA4 braucht. Mit Rufus/Ventoy auf einen Stick,
booten, „Try Ubuntu". Netzwerkkabel dran.

Kernel und GPU prüfen:

```bash
uname -r                                   # erwartet 6.17.x
lspci -nn | grep -i -E "vga|display"       # erwartet AMD ... [1002:7550] (Navi 48)
ls -l /dev/kfd /dev/dri/                   # beides muss existieren
dmesg | grep -i amdgpu | head -20          # kein "unsupported" / "failed"
```

Fehlt `/dev/kfd` oder meldet `dmesg` Fehler, bitte den Output zurückgeben, dann
ist der Live-Kernel zu alt — Ubuntu 25.10 Live als Ersatz nehmen.

### 0.2 Speicherplatz für Docker

Die Live-Session liegt im RAM. Modelle (≈ 17 GB) + Images (≈ 7 GB) passen da nicht
rein. Du brauchst eine Platte für `/var/lib/docker`. Zwei Wege:

**A) Externe SSD / zweiter USB-Stick (≥ 64 GB).** Egal welches Dateisystem, wird
ext4 formatiert:

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL,TRAN      # externe Platte identifizieren (TRAN=usb)
sudo mkfs.ext4 -F /dev/sdX                     # X = externe Platte, ALLES DARAUF GEHT VERLOREN
```

**B) Platte 2 (die spätere Ubuntu-Platte).** Nur wenn die Daten dort schon
gesichert sind — sie wird bei der Installation ohnehin gelöscht.

> **Platte 1 (Windows) nicht anfassen.** Beide Platten sind 256 GB. Unterscheiden:
> ```bash
> lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTLABEL
> sudo mkdir -p /mnt/chk
> for p in /dev/nvme0n1p1 /dev/nvme1n1p1 /dev/sda1 /dev/sdb1; do
>   sudo mount -o ro $p /mnt/chk 2>/dev/null && { echo "== $p"; ls /mnt/chk/EFI 2>/dev/null; sudo umount /mnt/chk; }
> done
> ```
> Die Platte, deren erste Partition `EFI/Microsoft` enthält, ist Windows. **Die andere** ist Platte 2.
> ```bash
> sudo wipefs -a /dev/nvmeXn1          # nur Platte 2!
> sudo mkfs.ext4 -F /dev/nvmeXn1
> ```

Dann einhängen und Docker darauf legen:

```bash
sudo mkdir -p /mnt/dock && sudo mount /dev/<platte> /mnt/dock
sudo apt-get update && sudo apt-get install -y docker.io ffmpeg python3 jq
sudo systemctl stop docker
sudo mkdir -p /etc/docker && echo '{"data-root":"/mnt/dock/docker"}' | sudo tee /etc/docker/daemon.json
sudo systemctl start docker
sudo usermod -aG docker $USER && newgrp docker
docker info | grep -i "docker root dir"        # erwartet /mnt/dock/docker
```

### 0.3 Spike-Dateien auf den PC

Dieses Verzeichnis (`docs/spike/`) auf den PC bringen — per USB-Stick oder:

```bash
git clone -b clipbox --depth 1 https://github.com/cccdemon/TWITCH-Generator.git /mnt/dock/tg
cd /mnt/dock/tg/docs/spike
```

### 0.4 Testmaterial: 30 min Audio + 64 Frames aus einem echten VOD

Ein Star-Citizen-VOD-Stück (≈ 35 min reichen). Entweder eine vorhandene `.mp4`
auf den Stick kopieren, oder direkt ziehen:

```bash
mkdir -p /mnt/dock/media && cd /mnt/dock/media
sudo apt-get install -y pipx && pipx install yt-dlp && export PATH=$PATH:~/.local/bin
yt-dlp -f "best[height<=1080]" --download-sections "*00:30:00-01:05:00" -o vod.mp4 "https://www.twitch.tv/videos/<VOD_ID>"

# 30-min WAV genau wie extract.py (16 kHz mono PCM)
ffmpeg -y -i vod.mp4 -t 1800 -vn -ac 1 -ar 16000 -c:a pcm_s16le chunk_0000.wav
# Grid-Frames genau wie vision.py (1 Frame / 10 s, 640 px breit), 64 Stück
mkdir -p frames && ffmpeg -y -i vod.mp4 -vf "fps=1/10,scale=640:-2" -q:v 5 -frames:v 64 frames/f_%05d.jpg
ls frames | wc -l   # 64
```

---

## 1. Ollama ROCm startet und sieht `gfx1201`

```bash
docker pull ollama/ollama:rocm
docker image inspect ollama/ollama:rocm --format '{{.Id}} {{.Created}}'    # notieren

docker run -d --name ollama --restart unless-stopped \
  --device /dev/kfd --device /dev/dri \
  --group-add $(getent group render | cut -d: -f3) \
  --group-add $(getent group video  | cut -d: -f3) \
  -e OLLAMA_MAX_LOADED_MODELS=1 -e OLLAMA_NUM_PARALLEL=1 \
  -e OLLAMA_FLASH_ATTENTION=1 -e OLLAMA_KV_CACHE_TYPE=q8_0 \
  -v ollama:/root/.ollama -p 11434:11434 ollama/ollama:rocm

sleep 5
docker logs ollama 2>&1 | grep -i -E "version|inference compute|gfx|rocm|library=" | head -20
docker exec ollama ollama --version
```

**Erwartet:** eine Zeile `inference compute ... library=rocm ... compute=gfx1201 ... name="AMD Radeon RX 9070 XT" ... total="16.0 GiB"`.
Das Image enthält kein `rocminfo`; diese Log-Zeile ist der Nachweis (so prüft später auch `bootstrap.sh`).

**Wenn stattdessen `library=cpu` oder gar keine GPU-Zeile:** vollständiges `docker logs ollama` zurückgeben, dazu `dmesg | grep -i -E "amdgpu|kfd" | tail -30`.

> Hinweis: `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` halbieren den KV-Cache. Ohne das passt `qwen3:14b` mit 32k Kontext voraussichtlich **nicht** in 16 GB. Schritt 3 testet beides.

---

## 2. Vision: `qwen3-vl:8b`, Triage-Schema per `format`

```bash
docker exec ollama ollama pull qwen3-vl:8b        # ≈ 6 GB
cd /mnt/dock/tg/docs/spike

# a) 1 Frame, 3 Läufe (erster Lauf enthält Modell-Ladezeit)
python3 vision_triage.py --frames /mnt/dock/media/frames --n 1  --runs 3
# b) 16 Frames pro Call = Prod-Batchgröße (max_frames_per_call), 3 Läufe
python3 vision_triage.py --frames /mnt/dock/media/frames --n 16 --runs 3
# c) während (b) läuft, in zweitem Terminal:
docker exec ollama ollama ps        # erwartet PROCESSOR "100% GPU", SIZE notieren
```

Das Skript druckt pro Lauf: Wandzeit, `prompt_eval_count`, `eval_count`, tok/s,
ob die Antwort valides JSON nach Schema ist und ob alle Frame-Indizes zurückkamen.
**Bitte die komplette Ausgabe zurückgeben** (am Ende steht ein Beispiel-Antwortobjekt).

Optional, falls (b) > 60 s pro Call dauert:

```bash
docker exec ollama ollama pull qwen3-vl:4b
python3 vision_triage.py --frames /mnt/dock/media/frames --n 16 --runs 3 --model qwen3-vl:4b
```

---

## 3. Text: `qwen3:14b` mit ≈ 25k-Token-Prompt bei `num_ctx 32768`

```bash
docker exec ollama ollama pull qwen3:14b           # ≈ 9 GB
cd /mnt/dock/tg/docs/spike

# a) Hauptmessung: passt 25k Prompt + 32k Kontext in VRAM? tok/s?
python3 text_ctx.py --target-tokens 25000 --num-ctx 32768
docker exec ollama ollama ps                        # SIZE + PROCESSOR notieren (100% GPU? oder z. B. "23%/77% CPU/GPU")
docker logs ollama 2>&1 | grep -i -E "truncat|exceed|offload|layers" | tail -10

# b) Kontrolle: Wie sieht Abschneiden aus? (num_ctx absichtlich zu klein)
python3 text_ctx.py --target-tokens 25000 --num-ctx 8192
docker logs ollama 2>&1 | grep -i -E "truncat|exceed" | tail -5

# c) Nur wenn (a) nicht komplett auf GPU läuft: ohne q8-KV-Cache ist es schlimmer,
#    dann Alternative 1: kleineres Modell
docker exec ollama ollama pull qwen3:8b
python3 text_ctx.py --target-tokens 25000 --num-ctx 32768 --model qwen3:8b
docker exec ollama ollama ps
```

Das Skript meldet: geschätzte Prompt-Tokens vs. `prompt_eval_count` (große Lücke =
abgeschnitten), Prompt-Verarbeitungs-Rate, Generier-tok/s, Wandzeit, JSON gültig?

---

## 4. whisper.cpp Vulkan-Server, 30-min-WAV

```bash
docker pull ghcr.io/ggml-org/whisper.cpp:main-vulkan
docker image inspect ghcr.io/ggml-org/whisper.cpp:main-vulkan --format '{{.Id}} {{.Created}}'

# Mesa-Version im Image (RDNA4 braucht Mesa >= 25.0 für RADV)
docker run --rm ghcr.io/ggml-org/whisper.cpp:main-vulkan sh -c "dpkg -l mesa-vulkan-drivers | tail -1"

docker run -d --name whisper --restart unless-stopped \
  --device /dev/dri \
  --group-add $(getent group render | cut -d: -f3) \
  --group-add $(getent group video  | cut -d: -f3) \
  -v whisper-models:/models -p 8080:8080 \
  ghcr.io/ggml-org/whisper.cpp:main-vulkan \
  sh -c 'test -f /models/ggml-large-v3-turbo.bin || download-ggml-model.sh large-v3-turbo /models; \
         exec whisper-server -m /models/ggml-large-v3-turbo.bin --host 0.0.0.0 --port 8080 -l de -t 6 -fa'

docker logs -f whisper          # warten bis "server listening"; Ctrl-C
docker logs whisper 2>&1 | grep -i -E "vulkan|device|gpu|9070|error" | head
```

**Erwartet:** `ggml_vulkan: Found 1 Vulkan devices: ... AMD Radeon RX 9070 XT (RADV GFX1201)`.
Wenn `llvmpipe` oder kein Device: Mesa im Image zu alt → Output zurückgeben (dann bauen wir ein eigenes Vulkan-Image auf Ubuntu 25.10-Basis).

Transkription messen:

```bash
cd /mnt/dock/tg/docs/spike
bash whisper_post.sh /mnt/dock/media/chunk_0000.wav verbose_json
# zur Sicherheit auch SRT (Fallback-Format):
bash whisper_post.sh /mnt/dock/media/chunk_0000.wav srt
```

Das Skript druckt Wandzeit, Echtzeit-Faktor (1800 s / Wandzeit), Top-Level-Keys der
Antwort, die ersten drei Segmente und speichert die Rohantwort nach
`/tmp/whisper_<format>.out`. **Beide Rohantworten (zumindest die ersten 60 Zeilen) zurückgeben.**

Optional zweiter Lauf ohne `-fa` wenn der erste Fehler wirft (Flash-Attention ist auf Vulkan nicht auf jeder Karte stabil).

---

## 5. Hochrechnung (mache ich aus euren Zahlen)

Aus Schritt 2b: Sekunden pro 16-Frame-Call `T16`.
6-h-VOD bei `interval_seconds: 15` → 1440 Frames → 90 Calls → Triage ≈ 90 × T16.
Dazu Tier-2-Bursts (≈ 20–60 Calls à 12 Bilder), Facecam (1 Call), analyze (≈ 36 Calls à 10-min-Chunk), refine (1 Call, 25k Tokens).
Whisper: 12 Chunks × (1800 / Faktor).
Ziel: Summe + Download + Render < 4 h.

---

## Aufräumen

```bash
docker rm -f ollama whisper
# Volumes behalten (Modelle, spart beim echten Setup nichts — dort kommt ein frisches System)
```

## Rückgabe an den Agenten

Am einfachsten: Terminal-Ausgabe aller Schritte in eine Datei:

```bash
script -q /mnt/dock/spike-$(date +%F).log      # ab hier wird alles mitgeschrieben
# ... Schritte 1–4 ...
exit
```

Diese Log-Datei plus `/tmp/whisper_verbose_json.out` (erste 60 Zeilen) zurückgeben.
