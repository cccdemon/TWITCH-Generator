# Clip-Bildqualität und Bildbereiche für Cam und Content

Stand: 2026-11-16, nachts. Noch **nichts umgesetzt**, nur Analyse. Auslöser: Vergleich
mit StreamLadder ("die Qualität ist richtig gut") und der Wunsch "Lass mich doch die
Bildbereiche wählen für Cam und Content".

## Befund (aus `src/clip.py`, `config.yaml`, `src/main.py`)

- **Facecam wird stark vergrößert.** Standard-Box = 22 % der Quellbreite, bei 1080p
  rund 420 × 240 px. Das Split-Layout füllt damit eine Zelle von 1080 × 960 (9:8) per
  `force_original_aspect_ratio=increase`: skaliert wird auf 960 px Höhe, also etwa
  **4-fach**, dann seitlich beschnitten. Skalierer ist ffmpegs Standard (bicubic).
  Vermutlich der größte sichtbare Unterschied.
- **Encoder:** überall `libx264 -preset veryfast -crf 20` (`render_clip`,
  `render_custom`, `trim_clip`). Für eine Vorlage, die TikTok/Instagram noch einmal
  kodieren, zu knapp.
- **Spielbereich in der Pipeline ist fest:** `_split_filtergraph` nimmt für die untere
  Hälfte immer den Mittelausschnitt. Nur die Cam-Box ist wählbar.
- Bildrate: kein fps-Filter, Download `best`. Sollte 60 fps behalten; an einem
  fertigen Clip noch nicht geprüft.

## Was es an Bereichswahl schon gibt

| Wo | Was | Gilt für |
|---|---|---|
| Clips-Seite, Panel „Posten" (Commit `f3c2609`) | Cam- und Spiel-Box über der Vorschau, Layout split/crop, in `posts.json` | einzelnen Clip, beim Neu-Rendern aus der Quelle |
| Editor `/edit/{vod}` | Cam- und Spiel-Box, Layout | von Hand geschnittene Clips |
| `/facecam/{vod}` | nur Cam-Box, in `facecam.json` (`manual: true`) | alle Clips des VODs beim nächsten Pipeline-Render |

**Lücke:** Es gibt keine Stelle, an der man Cam **und** Content einmal pro VOD (oder
als Standard für alle künftigen VODs) festlegt, sodass der automatische Render sie
benutzt. Genau das ist mit dem Wunsch gemeint (Annahme, morgen kurz bestätigen).

## Plan

### 1. Schärfere Skalierung
- In `clip.py` einen gemeinsamen Füll-Baustein: `scale=…:flags=lanczos` statt Standard.
- Cam-Zelle zusätzlich leicht nachschärfen (`unsharp`, mild), Spiel nicht.
- Betrifft `_split_filtergraph`, `_box_fill`, den crop-Zweig in `render_clip`.

### 2. Encoder
- `-preset slow -crf 17` als Vorgabe, einstellbar über `clip.preset` / `clip.crf` in
  `config.yaml` (beide Configs).
- Achtung Prod (LXC 105, 2 CPU): `slow` bei 1080×1920 ist ein Vielfaches langsamer als
  `veryfast`. Dort ggf. `medium` lassen. Auf der ClipBox unkritisch.
- `trim_clip` kodiert einen fertigen Clip neu (zweite Generation): gleiche Werte.

### 3. Neues Layout mit weniger Zoom
- Arbeitsname `stack`: Cam oben 1080 × 640 (ein Drittel, ≈ 16:9, die Cam-Box wird
  nicht mehr beschnitten), Spiel unten 1080 × 1280 (zwei Drittel).
- Cam-Vergrößerung sinkt damit von rund 4-fach auf rund 2,6-fach, das Spiel wird größer.
- Unscharfer Hintergrund entfällt, weil beide Zellen voll gefüllt sind.
- Stellen, die Layouts kennen und angepasst werden müssen: `posting.LAYOUTS`,
  `posting_web._crop_default` (Seitenverhältnis der Boxen), Crop-Editor in
  `clips_vod.html` (Boxen sind aufs Zellen-Seitenverhältnis gesperrt), `edit.html`,
  `web.py` `/render`, `main.py`, `config.yaml`. Am saubersten: Layout = Höhenanteil der
  oberen Zelle (split 0.5, stack 1/3, crop 0), daraus alle Seitenverhältnisse ableiten.

### 4. Bildbereiche für Cam und Content pro VOD
- `/facecam/{vod}` zur Seite „Bildbereiche" ausbauen: Cam-Box, Content-Box, Layout,
  auf dem Standbild aus `/frame/{vod}`, mit 9:16-Vorschau.
- Speichern in `facecam.json` (Cam-Schlüssel `x,y,w,h` bleiben, dazu `game` und
  `layout`), damit `vision.detect_facecam`, „Free MB" (`web._FREE_KEEP`) und das
  Post-Panel unverändert weiterlaufen.
- Häkchen „als Standard für neue VODs": zusätzlich nach `data/layout_default.json`;
  Reihenfolge dann VOD-Auswahl → Standard → Auto-Erkennung → `config.yaml`.
- Pipeline: `render_clip` bekommt `game=` und nutzt dieselbe Box-Logik wie
  `render_custom`; `main.py` reicht Layout und Boxen durch.
- Post-Panel: `_crop_default` startet mit diesen Boxen statt mit dem Mittelausschnitt.

### 5. Prüfen
- Tests: `tests/test_posting_render.py`, `tests/test_posting_web.py` prüfen Render-
  Aufrufe; neue Fälle für `stack` und für die Content-Box in der Pipeline.
- Ein Clip vorher/nachher auf der ClipBox rendern und nebeneinander ansehen (gleiche
  Stelle, altes und neues Layout), dabei fps mit `ffprobe` bestätigen.

## Offene Fragen für morgen
1. Bereichswahl pro VOD plus Standard für alle (Abschnitt 4), oder reicht die
   vorhandene Wahl pro Clip?
2. Layoutname und Aufteilung: ein Drittel / zwei Drittel recht so?
3. Prod auf `medium` lassen oder ebenfalls `slow`?
