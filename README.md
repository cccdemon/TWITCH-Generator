# TWITCH-Generator

Turn a Twitch VOD into short-form clips automatically.

```
Twitch VOD ──▶ Twitch API (metadata) ──▶ yt-dlp download
   ──▶ one ffmpeg pass: wav chunks + frame grid + loudness
   ──▶ Whisper transcription (faster-whisper)   ∥  in parallel:
         · CHAT   chat replay velocity (emote spam, "clip it", laughter)
         · AUDIO  loudness bursts above the stream's own level (ebur128)
         · VIDEO  Claude Vision, two-tier: Haiku triages every frame (10s grid),
                  Sonnet confirms hot spots from 3-frame bursts (sees motion)
   ──▶ TEXT   Claude scans the transcript for candidates
   ──▶ fuse:  highlight = wC·chat + wA·audio + wT·text + wV·video
   ──▶ refine: one Claude pass ranks all candidates side by side, drops filler,
               snaps start/end onto sentence boundaries
   ──▶ FFmpeg cuts 9:16 clips (facecam top / game bottom)
   ──▶ upload: YouTube Shorts · TikTok · Instagram Reels
```

Every signal is scored RELATIVE to a rolling baseline (±2.5 min), so a loud
stream or a busy chat is not "always exciting". Chat needs no API keys (public
replay). Weights, grid and thresholds live in `config.yaml` (`signals`, `chat`,
`audio`, `vision`, `score`, `refine`). Per-clip component scores (c/a/t/v) are
printed and saved in `moments.json`; `candidates.json` holds what the refine pass
saw.

Everything runs in one Docker container. Artifacts land in `./data/<vod_id>/`
(`transcript.json`, `moments.json`, `clips/*.mp4`).

## Quick start (web UI, standalone)

```bash
cp .env.example .env        # set USERS (logins) + SESSION_SECRET
docker compose build
docker compose up           # web UI -> http://localhost:9443
```

Log in with a user from `USERS`, then:
1. **Settings** — paste your keys (Anthropic, Twitch, upload tokens). Saved to
   `data/settings.json`, no need to touch `.env`. Optionally upload the YouTube
   `client_secret.json`.
2. **Select VODs** — type a streamer login → list their VODs → tick the ones you want
   (or paste a VOD url/id directly) → *Start processing*. Tick "render only" to skip upload.
3. **Jobs** — table auto-refreshes; click *log* for live pipeline output.

Jobs run **serially** (one at a time — Whisper + ffmpeg are heavy).

## Auth

Per-user login (username + password) with a signed session cookie.

```env
USERS=admin:s3cret,tom:hunter2     # comma-separated user:password pairs
SESSION_SECRET=<openssl rand -hex 32>   # keep stable, else restart forces re-login
```

> Secrets entered in the UI are stored unencrypted in `data/settings.json`. Always run
> behind TLS (the suite proxy provides it in prod).

## Production — co-hosted on the RDOC-Suite box (suite.raumdock.org/vod)

This is a **separate compose project** at `/opt/TWITCH-Generator` on LXC 103 (same box /
pattern as RDOC-LRC). It does not terminate TLS — the RDOC-Suite `caddy-rdoc` front door
(host network, `:9443`) routes `/vod` to it.

- `docker-compose.prod.yml` — app listens on `9443` in-container, published loopback-only
  as `127.0.0.1:9444`, `ROOT_PATH=/vod`.
- The Caddy route block lives in `RDOC-Suite/deploy/caddy-rdoc/Caddyfile`
  (`handle_path /vod*` → `127.0.0.1:9444`).

Deploy (inside LXC 103):

```bash
cd /opt/TWITCH-Generator
docker compose -f docker-compose.prod.yml up -d --build      # bring up first (port 9444)

cd /opt/RDOC-Suite                                            # then reload the proxy
docker compose -f docker-compose.prod.yml up -d caddy-rdoc
```

Then open `https://suite.raumdock.org/vod` and log in.

## CLI (no UI)

```bash
# render + upload per config.yaml
docker compose run --rm twitch-generator run --vod https://www.twitch.tv/videos/123456789

# render only, no upload (good first test)
docker compose run --rm twitch-generator run --vod 123456789 --no-upload
```

The CLI reads the same `data/settings.json` the web UI writes, so keys entered in the UI
work for CLI runs too.

## Configuration

- `config.yaml` — tunables (Whisper model, clip length, categories, platforms). Mounted read-only.
- `.env` — secrets. See `.env.example`.

| Var | Needed for |
|-----|-----------|
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | VOD title/duration metadata (download works without it) |
| `ANTHROPIC_API_KEY` | LLM moment detection (required) |
| `YOUTUBE_CLIENT_SECRETS` / `YOUTUBE_TOKEN_FILE` | YouTube upload |
| `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` (+ `PUBLIC_URL`) | TikTok Connect (OAuth) |
| `INSTAGRAM_ACCESS_TOKEN` / `INSTAGRAM_USER_ID` | Instagram Reels upload |

## Performance

Whisper is the slow stage. Defaults are CPU/`int8` — fine but slow on long VODs.
For GPU: use a CUDA base image, set `whisper.device: cuda` + `compute_type: float16`
in `config.yaml`, uncomment the `deploy.resources` block in `docker-compose.yml`,
and run with the NVIDIA container runtime. Drop `whisper.model` to `medium`/`small`
to trade accuracy for speed. The Whisper model is cached in the `whisper-cache` volume.

Vision cost: Haiku triages every grid frame (3h VOD at 10s ≈ 1080 frames ≈
70 calls, well under $1), Sonnet only sees bursts around hot spots. Raise
`vision.interval_seconds`, disable `vision.burst`, or set `vision.enabled: false`
to cut cost. The refine pass is one Sonnet call per VOD. Frames, transcript, chat
and loudness are cached per VOD under `data/<vod_id>/`, so re-runs skip them.
Whisper runs with `beam_size: 1`; chat/audio/vision run in parallel with it.

## Upload caveats (read before expecting magic)

- **YouTube** — fully working. First run needs an interactive OAuth consent:
  ```bash
  docker compose run --rm -it twitch-generator run --vod <id>
  ```
  Paste the consent code once; the token is saved to `YOUTUBE_TOKEN_FILE` for reuse.
  Use a Desktop-app OAuth client from Google Cloud Console.
- **TikTok** — Content Posting API, two modes (`upload.tiktok.mode` in `config.yaml`):
  - `draft` (default): the clip is uploaded into your TikTok **inbox**; you open the app,
    add the caption and post. Works without TikTok's app audit and on public accounts.
    Also available per clip via the **▶ TikTok** button on the clips page.
  - `direct`: Direct Post. Unaudited apps may only post `SELF_ONLY` to a private account;
    public Direct Post needs TikTok's app audit (`video.publish`).
  Setup: create a TikTok developer app with Login Kit + Content Posting API, add
  `<PUBLIC_URL>/oauth/tiktok/callback` as redirect URI, enter client key/secret in Settings,
  click **Connect** under *Connected accounts*. The developer portal needs public
  Privacy Policy / Terms URLs: use `<PUBLIC_URL>/privacy` and `<PUBLIC_URL>/terms`
  (served without login; set `OPERATOR_NAME` / `CONTACT_EMAIL` in `.env`).
- **Instagram Reels** — Graph API needs a *public* video URL, not a file upload. Host the
  clip on a CDN/bucket and pass its URL. Needs a Business/Creator IG account linked to a
  Facebook Page. No-ops with a message if token/URL missing.

There is no official, frictionless "just upload a file" API for TikTok or Instagram —
both gate behind app review. The code is structured so once you have valid tokens it works.

## Stages

`src/twitch.py` download · `src/extract.py` single decode pass · `src/transcribe.py` Whisper ·
`src/chat.py` chat velocity · `src/audio.py` loudness · `src/vision.py` Claude Vision (two-tier) ·
`src/analyze.py` transcript candidates · `src/score.py` fusion · `src/refine.py` global rerank ·
`src/clip.py` FFmpeg · `src/upload.py` platforms · `src/main.py` orchestrator.
