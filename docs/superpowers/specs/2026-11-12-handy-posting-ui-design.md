# Handy-Posting im Generator-UI — Design

Stand: 2026-11-12 · Branch `clipbox`

## Warum

Für das Posten auf TikTok und Instagram wurde extra ein Handy angeschafft. Der Dienst, der es steuert (`poster/`, „clip-poster", FastAPI auf dem Phone-Host im LAN, Port 3300, adb), existiert schon und kann posten. Im Generator-UI gibt es aber keinerlei Bedienung dafür. Ziel: **Clips posten, ohne das Handy je anzufassen**, wobei der Nutzer jeden Clip selbst freigibt.

## Ziele

- Der Nutzer bestimmt pro Clip, ob er rausgeht. Nichts wird automatisch gepostet.
- Vor dem Senden prüft er Text, Hashtags und Links und kann sie ändern.
- Er kann den Clip **grafisch** nachschneiden (ziehbare Griffe auf einer Zeitleiste, keine ±-Sekunden-Knöpfe), auch **früher anfangen / später aufhören** als der automatische Schnitt.
- Er sieht den geschnittenen Clip als **Vorschau**, bevor er gesendet wird.
- Plattformen TikTok und/oder Instagram, Zeitpunkt sofort oder geplant.
- Status pro Clip sichtbar: geplant / läuft / gepostet / teilweise / Fehler, inkl. Screenshot vom Handy.

## Nicht-Ziele

- Vollautomatisches Posten ohne Freigabe.
- Änderungen an den Handy-Flows (`poster/app/flows.py`, `phone.py`). Der Dienst wird so genutzt, wie er ist; kleine API-Ergänzungen nur, wenn zwingend nötig.
- Posting-Kalender / automatische Verteilung auf Zeitslots.
- Neue Plattformen.

## Anti-Ziele (wäre ein Fehlschlag, auch wenn's technisch läuft)

- Gesendet wird etwas anderes, als der Nutzer in der Vorschau gesehen hat.
- Der Nutzer muss doch ans Handy, um etwas zu prüfen oder zu reparieren, was das UI hätte zeigen können.
- Das Schneiden fühlt sich an wie Zahlen-Tippen.

## Bestand (relevant)

- **poster-API** (`poster/app/main.py`, Bearer-Token `POSTER_TOKEN`):
  `GET /api/status` (online, Akku, busy, queued, next_run), `POST /api/posts` (multipart: `video` + `meta`-JSON mit `platforms`, `caption`, `clip_name`, `title`, `source`, `run_at`, `threads`, `dry_run`), `GET /api/posts[/{id}]`, `PATCH /api/posts/{id}` (caption, run_at; nur `queued`), `POST …/cancel`, `POST …/retry`, `DELETE …`, `GET …/shot/{platform}` (Screenshot). Validiert Insta ≤ 2200 Zeichen / ≤ 30 Hashtags, TikTok ≤ 4000. `dry_run` = „Probelauf" (alles außer dem letzten Tippen).
- **Clip-Seite** `GET /clips/{vod_id}` (`src/templates/clips_vod.html`, Daten aus `web._clips_detail`): Liste der gerenderten Clips mit Video, Titel, Scores, Download, optional alter TikTok-API-Knopf. Clip-Dateiname beginnt mit `int(moment.start)`, darüber wird der Moment aus `moments.json` zugeordnet (`title`, `category`, `start`, `end`).
- **Rendern** mit freiem Fenster: `clip.render_custom(...)` (benutzt vom Editor über `POST /render/{vod_id}`), Quelle `data/<vod>/<vod>.mp4`, Facecam-Box aus `facecam.json` / `vision.detect_facecam` / `config.yaml`.
- **Signale** für die Zeitleiste liegen als `audio.json` / `chat.json` (`[(bucket_start_s, score)]`) und `transcript.json` im VOD-Ordner.
- „Free MB" (`POST /vods/{vod_id}/free`) löscht das Quell-VOD, Clips bleiben.

## Entschieden (vom Nutzer)

1. **Ort:** direkt auf der Clip-Seite eines VODs (Variante A aus dem Entwurf). Pro Clip ein Knopf „📱 Posten", der darunter ein Feld aufklappt. Oben auf der Seite eine Statuszeile zum Handy.
2. **Schneiden grafisch:** Zeitleiste mit zwei ziehbaren Griffen (In/Out) und Abspielkopf. Links und rechts ±20 s Material aus dem Original-VOD (grau), damit der Clip auch länger werden kann.
3. **Vorschau, zweistufig:**
   - Sofort-Vorschau: spielt nur den Bereich In→Out ab (Schleife), ohne Rendern.
   - „Fertigen Clip rendern": rendert die 9:16-Version mit dem neuen Schnitt. **Gesendet wird immer genau diese gerenderte Datei.**
4. **Text:** schon ausgefüllt vorgeschlagen, frei änderbar. Er besteht aus Clip-Titel, Twitch-Link und Hashtags.
5. **Hashtags:** beides — eine **feste Liste** (einmal in den Settings gepflegt) **plus** pro Clip ein paar **von der KI passend zum Inhalt** ausgesuchte.
6. **Plattform:** TikTok und/oder Instagram, dazu Probelauf. **Threads** bekommt einen eigenen Haken pro Clip (nur aktiv, wenn Instagram gewählt ist; geht als `threads` an den Dienst). Startwert: an, wie bisher im Dienst; danach merkt sich das UI die letzte Wahl (Designer-Entscheidung). **Zeitpunkt:** sofort oder feste Uhrzeit.

## Vom Designer entschieden (bitte prüfen)

- **Ohne Quell-VOD** (nach „Free MB") ist nur Kürzen innerhalb des vorhandenen Clips möglich; die grauen Ränder entfallen. „Free MB" wird nicht blockiert, aber der Bestätigungsdialog erwähnt, dass danach kein Verlängern mehr geht, wenn ungepostete Clips existieren.
- **Signal-Leiste** unter der Zeitleiste: Lautstärke- und Chat-Ausschläge aus `audio.json`/`chat.json`, zur Orientierung. Optional: Griffe rasten an Satzgrenzen aus `transcript.json` ein (abschaltbar).
- **Neu gerenderter Clip ersetzt die Clip-Datei** unter demselben Dateinamen (Zuordnung zum Moment bleibt). Das gewählte In/Out wird gespeichert, damit Zeitleiste und Vorschau beim nächsten Öffnen stimmen. „Zurücksetzen" rendert das ursprüngliche Fenster aus `moments.json` (inkl. `clip.pad_start/pad_end`) neu.
- **Render-Parameter** für die Neu-Version wie beim Pipeline-Render: `clip.layout`, Facecam-Box in derselben Priorität, Untertitel gemäß `clip.burn_subtitles`.
- **Verbindung zum Handy-Dienst:** `POSTER_URL` und `POSTER_TOKEN` als Einträge in der Settings-Seite (`settings_store.SETTING_KEYS`, Token geheim). Der Browser redet nie direkt mit dem Handy-Dienst; der Generator leitet über eigene Endpunkte weiter, das Token bleibt serverseitig. Ohne konfigurierte URL ist der „📱 Posten"-Knopf ausgeblendet bzw. zeigt einen Hinweis auf die Settings.
- **Text-Vorlage, feste Hashtags, Twitch-Link** ebenfalls in den Settings (nicht geheim), da `config.yaml` im Container read-only ist. Vorlage z. B. `{title}\nLive: {twitch}\n{hashtags}`.
- **KI-Hashtags** werden beim ersten Aufklappen des Post-Felds per `llm.structured` (Rolle Text, `meter=`/`cfg=` wie üblich) aus Titel, Kategorie und dem Transkript-Ausschnitt des Clips erzeugt und pro Clip zwischengespeichert. Knopf „neu vorschlagen". Schlägt der Aufruf fehl, kommt nur die feste Liste; das Senden blockiert nie daran. Die KI-Tags werden mit der festen Liste ohne Duplikate zusammengeführt.
- **Zähler** unter dem Text: Zeichen je Plattform-Limit, Hashtag-Anzahl (Insta ≤ 30), Anzahl Links. Überschreitung macht den Senden-Knopf rot und blockiert ihn für die betroffene Plattform (dieselben Grenzen wie der Dienst).
- **Pro-Clip-Zustand** (Schnitt, Text-Entwurf, KI-Tags, poster-Post-ID(s)) in `data/<vod>/posts.json`. Ein Clip kann mehrfach gesendet werden (z. B. erst Probelauf, dann echt); angezeigt wird der jüngste Post, ältere als kurze Historie.
- **Status-Anzeige** pollt nur, solange auf der Seite etwas geplant/laufend ist. Geplante Posts: Uhrzeit und Text noch änderbar (PATCH), Abbrechen; Fehlgeschlagene: Wiederholen; Ergebnis mit Screenshot-Vorschau.
- **Geplanter Post sperrt den Schnitt:** Der Dienst kopiert das Video beim Anlegen und kann es danach nicht mehr tauschen. Solange ein Post des Clips `queued` oder `running` ist, sind Schneiden, Rendern und Zurücksetzen gesperrt, mit dem Hinweis „erst Post abbrechen". So kann nie die alte Fassung rausgehen, während die Seite die neue zeigt. Keine API-Erweiterung am Dienst.
- **Editor-Clips** (`<start>_edit_<slug>.mp4`, ohne Eintrag in `moments.json`, Zuschnitt nicht gespeichert): lassen sich posten, aber nur **innerhalb des Clips kürzen**, also die vorhandene 9:16-Datei neu kodieren. Kein Verlängern, kein Zurücksetzen auf ein Moment-Fenster. Text-Vorschlag nimmt den Titel aus dem Dateinamen.
- **Pipeline-Clips beim Neu-Rendern** verwenden die *aktuelle* Facecam-Box. Das ist bewusst so, weil die Render-Vorschau genau das zeigt, was rausgeht.
- **„Free MB" behält die kleinen Dateien**, die das Post-Feld braucht: `posts.json`, `transcript.json`, `audio.json`, `chat.json`, `facecam.json` (wenige hundert KB). Signal-Leiste, Einrasten und KI-Hashtags funktionieren danach weiter, nur die grauen Ränder (Verlängern) entfallen.
- **Sicherung beim Kürzen ohne Quelle:** Wird ein Clip ohne Quell-VOD (freigegebenes VOD oder Editor-Clip) gekürzt, bleibt die vorherige Datei einmalig als `clips/.orig/<name>.mp4` erhalten. „Zurücksetzen" stellt sie wieder her. Die Clip-Liste ignoriert `.orig/`.
- **Alter TikTok-API-Knopf** bleibt wie jetzt (nur sichtbar, wenn TikTok-OAuth verbunden ist).
- Sprache der neuen UI-Texte: Deutsch, wie im Handy-Dienst.

## Dem Builder überlassen

- Technik der Zeitleiste (Canvas/SVG/DOM), Vorschaubilder auf der Leiste ja/nein, genaue Optik — passend zu den bestehenden Inline-Templates (kein Static-Ordner, JS/CSS inline).
- Wie die Sofort-Vorschau das Quellmaterial abspielt (vorhandenes `/source/{vod_id}` mit Range-Support bzw. Proxy aus `/proxy/{vod_id}`); ob sie schon 9:16 zuschneidet oder das Querformat zeigt.
- Ob das Rendern synchron im Request läuft oder als kleiner Hintergrund-Job mit Fortschritt; Verhalten, wenn gerade eine Pipeline läuft.
- Endpunkt-Namen und Form von `posts.json`.
- Zeitzonen-Handling für „geplant" (Browser-Zeit → Unix-Zeit für `run_at`).
