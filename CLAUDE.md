# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Twitch VOD → short-form clips. Python 3.11, runs as one Docker container. Two entry points, both via `python -m src.main`:

- `web` (default CMD) — FastAPI UI on `:9443` (`src/web.py`), per-user login, job queue.
- `run --vod <url|id> [--no-upload] [--config path]` — one-shot CLI pipeline (`src/main.py:run_pipeline`).

No test suite, no linter config. Verification = run the pipeline on a VOD and inspect `data/<vod_id>/` (`candidates.json` vs `moments.json` shows what refine kept). Pure logic (`signals`, `chat`, `audio`, `score`, `refine`) imports without ffmpeg or Whisper and can be exercised with plain Python plus `pyyaml`/`anthropic` installed.

## Commands

```bash
docker compose build
docker compose up                                   # web UI → http://localhost:9443
docker compose run --rm twitch-generator run --vod 123456789 --no-upload   # CLI, render only
docker compose run --rm -it twitch-generator run --vod <id>               # first YouTube upload needs -it (OAuth consent)
```

Local without Docker: `pip install -r requirements.txt`, needs `ffmpeg`/`ffprobe` on PATH, then `python -m src.main web` or `... run --vod ...`. `TG_DATA_DIR` (default `./data`) and `TG_CONFIG` (default `config.yaml`) override paths.

Prod (co-hosted, `suite.raumdock.org/vod`): `docker compose -f docker-compose.prod.yml up -d --build`, then reload `caddy-rdoc` in the RDOC-Suite project. Dedicated host (`vod.raumdock.org`): `docker-compose.dedicated.yml` + `Caddyfile.dedicated`. See README for details.

## Architecture

### Pipeline (src/main.py)

Stages are modules with one public function each. Every stage caches its output as JSON under `data/<vod_id>/` and skips work if the cache (with matching params) exists, so a retry resumes from the first uncached stage:

| Step | Module | Cache file |
|---|---|---|
| resolve + download | `twitch.py` | `info.json`, `<vod_id>.mp4` |
| single-pass decode | `extract.py` | writes `audio_chunks/*.wav`, `frames/f_*.jpg` + `meta.json`, `loudness.json` in one ffmpeg run; purely an accelerator, every consumer still extracts on its own if missing |
| Whisper | `transcribe.py` | `transcript.json` |
| chat velocity | `chat.py` | `chat_messages.json` (raw replay via chat-downloader), `chat.json` |
| audio bursts | `audio.py` | `loudness.json` (raw ebur128), `audio.json` |
| vision, two-tier | `vision.py` | `vision_triage.json` (Haiku, every grid frame), `frames/bursts/`, `vision.json` (Sonnet-confirmed), `facecam.json` |
| text candidates (Claude) | `analyze.py` | none |
| fusion | `score.py` | `candidates.json` |
| refine (Claude, one global call) | `refine.py` | `moments.json` |
| render | `clip.py` | `clips/*.mp4` (+ `.srt`) |
| upload | `upload.py` | — |

Whisper runs in the main thread while chat, audio and vision run in a thread pool (they are network/API bound). Chat/audio/vision failures are logged and degrade to an empty signal; they never abort the run.

**Signals** (`signals.py`): a signal is `[(bucket_start_s, score 0..1)]`. Scores are always relative to a rolling median over `signals.baseline_window_seconds`, never absolute, because streams are loudness-normalised and chats are busy all night. `relative()`, `clusters()`, `max_in()` are the shared primitives. Keep any new signal on this contract so `score.fuse` picks it up unchanged.

**Fusion** (`score.py`): candidate windows come from text moments, vision clusters and chat clusters (audio never spawns one). Each candidate gets `wC·chat + wA·audio + wT·text + wV·video`; the top `max_clips × candidate_factor` go to `refine`. `Moment.hints` carries vision labels and peak times into the refine prompt.

**Refine** (`refine.py`): one Claude call sees all candidates with transcript context, re-scores on a shared rubric, may drop everything, and moves boundaries. `snap_to_segments` then snaps start earlier / end later onto transcript segment edges. On API failure it falls back to the fused ranking plus snapping.

Claude calls use tool-use for structured output and feed `usage` into a `pricing.CostMeter`; totals go to `cost.json`. `timing.py` keeps a learned processing-time factor in `data/timing.json` (EMA) and writes per-run `run.json` for the UI's ETA. When adding a Claude call, pass `meter=` through and add the model to `pricing._PRICES` if new. Concurrent calls must lock around `meter.add` (see `vision._call_batches`).

Shared dataclasses live in `src/models.py` (`TranscriptSegment`, `VodInfo`, `VisionFrame`, `Moment`, `Clip`, `UploadResult`).

### Config and secrets

- `config.yaml` = tunables, read via `load_config()` as a plain dict; older stages receive explicit kwargs from `main.py`, newer ones (`chat`, `audio`, `vision`, `score`, `refine`) take `cfg` and read their own section. Cache files store the params they were built with and rebuild on mismatch. Mounted read-only in Docker. Note `clip.auto_render: false` stops the pipeline after `moments.json` so clips are rendered by hand in the web editor.
- Secrets = env vars read through `config.env()`. `settings_store.py` persists UI-entered keys to `data/settings.json` and `apply_settings()` pushes them into `os.environ` at startup (web) and at CLI run start. UI values override `.env`.
- OAuth tokens for upload platforms: `data/oauth.json` via `src/oauth.py` (`/oauth/<platform>/start|callback`). Redirect URI is built from `PUBLIC_URL`.

### Web layer (src/web.py, src/jobs.py)

- `JobManager` (singleton `manager`) runs jobs serially (`ThreadPoolExecutor(max_workers=1)`). Each job is a **separate subprocess**: `nice -n 19 python -u -m src.main run --vod ...` with `cwd="/app"` and its own process group. Cancel = `killpg`. Log lines are captured into the job and persisted to `data/jobs.json` (throttled). On restart, running/queued jobs become `interrupted`. This subprocess design is why the web process never imports Whisper. The `cwd="/app"` and `nice` assume the container.
- Auth: `USERS` env (`user:pass,...`), signed cookie via `SessionMiddleware` + `_require_login` middleware. Middleware add order at the bottom of `web.py` matters (last added runs first).
- `ROOT_PATH` (e.g. `/vod`): the reverse proxy strips the prefix, so routes stay unprefixed. Use `BASE` (from `root_path()`) only when building outward links/redirects in handlers and templates.
- Editor (`/edit/{vod_id}`): browser-side trim/crop over the source video, served with HTTP range support from `/source/{vod_id}`; an optional low-res proxy is built via `/proxy/{vod_id}`. `/render/{vod_id}` calls `clip.render_custom` with user-chosen facecam/game boxes. `/facecam/{vod_id}` lets the user override the auto-detected facecam box (stored in `facecam.json`, overrides `vision.detect_facecam`).
- Templates: Jinja2 in `src/templates/`, no static dir; JS/CSS inline in the templates.

### Vertical layout

`clip.vertical` + `clip.layout: split` renders top half = facecam region, bottom half = center-cropped game, via one ffmpeg `filter_complex` (`clip._split_filtergraph`). Facecam box is fractions of the source frame, from (in priority) UI pick → `vision.detect_facecam` → `config.yaml` `clip.facecam`.

## Gotchas

- Whisper on CPU is the bottleneck; `whisper.cpu_threads`, `chunk_seconds` and `beam_size: 1` are tuned for a 4-core box with 2 CPU / 4 GB caps in prod. Keep the chunking when touching `transcribe.py`.
- ffmpeg's `ebur128` line format includes a `TARGET:-23 LUFS` field between `t:` and `M:`; the regex in `audio.py` must keep tolerating it (the old one silently matched nothing).
- The app was tuned for Star Citizen content; vision categories and the system prompt in `vision.py` are game-specific.
- Secrets in `data/settings.json` and `data/oauth.json` are plaintext; `data/` is gitignored.
