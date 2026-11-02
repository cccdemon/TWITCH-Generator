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
docker compose run --rm twitch-generator run --vod <id>                   # render + upload to config.yaml upload.platforms
```

Local without Docker: `pip install -r requirements.txt`, needs `ffmpeg`/`ffprobe` on PATH, then `python -m src.main web` or `... run --vod ...`. `TG_DATA_DIR` (default `./data`) and `TG_CONFIG` (default `config.yaml`) override paths.

Prod (co-hosted, `suite.raumdock.org/vod`): `docker compose -f docker-compose.prod.yml up -d --build`, then reload `caddy-rdoc` in the RDOC-Suite project (its route block lives there, not in this repo). Dedicated host (`vod.raumdock.org`): `docker-compose.dedicated.yml` + `Caddyfile.dedicated` + `deploy/caddy/Dockerfile`. `WEB_PORT` (default 9443) sets the listen port. See README for details.

## Architecture

### Pipeline (src/main.py)

Stages are modules with one public function each. Every stage caches its output as JSON under `data/<vod_id>/` and skips work if the cache (with matching params) exists, so a retry resumes from the first uncached stage:

| Step | Module | Cache file |
|---|---|---|
| resolve + download | `twitch.py` | `info.json`, `<vod_id>.mp4` |
| single-pass decode | `extract.py` | writes `audio_chunks/*.wav`, `frames/f_*.jpg` + `meta.json`, `loudness.json` in one ffmpeg run; purely an accelerator, every consumer still extracts on its own if missing |
| Whisper | `transcribe.py` | `transcript.json` (backend `faster-whisper` in-process, or `whispercpp-server` over HTTP) |
| chat velocity | `chat.py` | `chat_messages.json` (raw replay via Twitch public GQL, hard-coded web client id + persisted-query hash, no API key), `chat.json` |
| audio bursts | `audio.py` | `loudness.json` (raw ebur128), `audio.json` |
| vision, two-tier | `vision.py` | `vision_triage.json` (cheap model, every grid frame), `frames/bursts/`, `vision.json` (strong-model confirmed), `facecam.json` |
| text candidates (LLM) | `analyze.py` | none |
| fusion | `score.py` | `candidates.json` |
| refine (LLM, one global call) | `refine.py` | `moments.json` |
| render | `clip.py` | `clips/*.mp4` (+ `.srt`) |
| upload | `upload.py` | — |

Whisper runs in the main thread while chat, audio and vision run in a thread pool (they are network/API bound). Chat/audio/vision failures are logged and degrade to an empty signal; they never abort the run.

**Signals** (`signals.py`): a signal is `[(bucket_start_s, score 0..1)]`. Scores are always relative to a rolling median over `signals.baseline_window_seconds`, never absolute, because streams are loudness-normalised and chats are busy all night. `relative()`, `clusters()`, `max_in()` are the shared primitives. Keep any new signal on this contract so `score.fuse` picks it up unchanged.

**Fusion** (`score.py`): candidate windows come from text moments, vision clusters and chat clusters (audio never spawns one). Each candidate gets `wC·chat + wA·audio + wT·text + wV·video`; the top `max_clips × candidate_factor` go to `refine`. `Moment.hints` carries vision labels and peak times into the refine prompt.

**Refine** (`refine.py`): one Claude call sees all candidates with transcript context, re-scores on a shared rubric, may drop everything, and moves boundaries. `snap_to_segments` then snaps start earlier / end later onto transcript segment edges. On API failure it falls back to the fused ranking plus snapping.

**Model calls** (`llm.py`): every structured model call goes through `llm.structured(role, system, content=[TextPart|ImagePart], schema, tool_name, cfg, meter, model=<stage's Claude model>)` and returns the parsed dict. `llm.provider` in `config.yaml` selects the backend: `anthropic` (default) = tool-use with forced `tool_choice`, model from the stage section, exactly the pre-`llm.py` requests; `ollama` = `POST /api/chat` with `format=<schema>`, `think: false`, temperature 0, per-role `num_ctx`, models from `llm.ollama.text_model|vision_model` (the Claude names are ignored), usage metered as `ollama/<model>` which `pricing` rates at $0. Never call the Anthropic SDK from a stage directly; the whole local deployment runs without `ANTHROPIC_API_KEY`. `llm.model_for()` gives the effective model name for cache params; `llm.vision_concurrency()` caps vision at 1 under ollama.

Usage from each call feeds a `pricing.CostMeter`; totals go to `cost.json`. `timing.py` keeps a learned processing-time factor in `data/timing.json` (EMA) and writes per-run `run.json` for the UI's ETA. When adding a model call, pass `meter=` and `cfg=` through and add new Claude models to `pricing._PRICES`. Concurrent calls must lock around `meter.add` (see `vision._call_batches`).

Shared dataclasses live in `src/models.py` (`TranscriptSegment`, `VodInfo`, `VisionFrame`, `Moment`, `Clip`, `UploadResult`).

### Upload and OAuth (src/upload.py, src/oauth.py)

- Default is **download-first**: `config.yaml` `upload.platforms: []`, so the pipeline renders and the user downloads from the clips page. `upload_clip(clip, platforms, ...)` dispatches per platform name (`youtube | tiktok | instagram`) and returns `UploadResult`s; a non-connected platform no-ops with a message instead of raising.
- All three platforms authenticate via the web UI: `/oauth/{platform}/start|callback`, tokens in `data/oauth.json`, refreshed on use by `oauth.token_for`. App credentials come from env (`YOUTUBE_CLIENT_ID/SECRET`, `TIKTOK_CLIENT_KEY/SECRET`, `FACEBOOK_APP_ID/SECRET`); `oauth.configured()` also requires `PUBLIC_URL`, since the redirect URI is `<PUBLIC_URL>/oauth/<platform>/callback`. There is no CLI consent flow anymore.
- TikTok has two modes in `upload.tiktok.mode`: `draft` (inbox upload, works without app audit) and `direct` (Direct Post, `SELF_ONLY` unless audited). `upload.tiktok_send` is also called per clip from `POST /clips/{vod_id}/{filename}/tiktok`.
- Instagram needs a public video URL (Graph API), so it only works when the clip is hosted somewhere reachable.

### Config and secrets

- `config.yaml` = tunables, read via `load_config()` as a plain dict; older stages receive explicit kwargs from `main.py`, newer ones (`chat`, `audio`, `vision`, `score`, `refine`) take `cfg` and read their own section. Cache files store the params they were built with and rebuild on mismatch. Mounted read-only in Docker. Note `clip.auto_render: false` stops the pipeline after `moments.json` so clips are rendered by hand in the web editor.
- Secrets = env vars read through `config.env()`. `settings_store.py` persists UI-entered keys (`SETTING_KEYS`: Anthropic, Twitch, TikTok) to `data/settings.json` and `apply_settings()` pushes them into `os.environ` at startup (web) and at CLI run start. UI values override `.env`. The Settings page marks the Anthropic key "nicht benötigt" when `llm.provider` is `ollama` (`web._llm_provider`).
- `llm:` and `whisper.backend/server_url` in the main `config.yaml` carry the prod defaults (`anthropic`, `faster-whisper`) and must stay that way; `deploy/local/config.yaml` is the local copy with `ollama` / `whispercpp-server`.

### Local appliance (deploy/local/, "ClipBox")

Home-PC deployment with zero cloud tokens: `docker-compose.local.yml` runs `app` (CPU), `ollama` (`ollama/ollama:rocm`, `/dev/kfd` + `/dev/dri`, one model at a time), `ollama-init` (one-shot `ollama pull`), `whisper` (`ghcr.io/ggml-org/whisper.cpp:main-vulkan`, `whisper-server`, model fetched by `whisper-entrypoint.sh`). `bootstrap.sh` (systemd oneshot `clipbox-bootstrap.service` on first boot, idempotent) writes `.env`, sets Docker log rotation, brings compose up, waits for the model pull and proves GPU use. `deploy/local/usb/` holds the Ventoy + Ubuntu autoinstall installer (`user-data`, `ventoy.json`, `build-usb.ps1`, German README). Operations doc: `deploy/local/README.md`; requirement: `docs/requirements/2026-11-02-anforderung-clipbox.md`; GPU measurements: `docs/spike-results.md`.

### Web layer (src/web.py, src/jobs.py)

- `JobManager` (singleton `manager`) runs jobs serially (`ThreadPoolExecutor(max_workers=1)`). Each job is a **separate subprocess**: `nice -n 19 python -u -m src.main run --vod ...` with `cwd="/app"` and its own process group. Cancel = `killpg`. Log lines are captured into the job and persisted to `data/jobs.json` (throttled). On restart, running/queued jobs become `interrupted`. This subprocess design is why the web process never imports Whisper. The `cwd="/app"` and `nice` assume the container.
- Auth: `USERS` env (`user:pass,...`), signed cookie via `SessionMiddleware` (key = `SESSION_SECRET`, random per start if unset, so every restart logs everyone out) + `_require_login` middleware. Only `/login`, `/health`, `/privacy`, `/terms` are public; `/api/*` returns 401 instead of redirecting. Middleware add order at the bottom of `web.py` matters (last added runs first).
- `/privacy` and `/terms` (template `legal.html`, `OPERATOR_NAME` / `CONTACT_EMAIL` env) exist because platform developer portals require reachable legal URLs. Keep them login-free.
- `ROOT_PATH` (e.g. `/vod`): the reverse proxy strips the prefix, so routes stay unprefixed. Use `BASE` (from `root_path()`) only when building outward links/redirects in handlers and templates.
- Editor (`/edit/{vod_id}`): browser-side trim/crop over the source video, served with HTTP range support from `/source/{vod_id}`; an optional low-res proxy is built via `/proxy/{vod_id}`. `/render/{vod_id}` calls `clip.render_custom` with user-chosen facecam/game boxes. `/facecam/{vod_id}` lets the user override the auto-detected facecam box (stored in `facecam.json`, overrides `vision.detect_facecam`).
- Disk: `POST /vods/{vod_id}/free` deletes source + intermediates but keeps clips/moments/cost/info; `/delete` wipes the whole VOD dir.
- Templates: Jinja2 in `src/templates/`, no static dir; JS/CSS inline in the templates.

### Vertical layout

`clip.vertical` + `clip.layout: split` renders top half = facecam region, bottom half = center-cropped game, via one ffmpeg `filter_complex` (`clip._split_filtergraph`). Facecam box is fractions of the source frame, from (in priority) UI pick → `vision.detect_facecam` → `config.yaml` `clip.facecam`.

## Gotchas

- Whisper on CPU is the bottleneck in prod; `whisper.cpu_threads`, `chunk_seconds` and `beam_size: 1` are tuned for a 4-core box with 2 CPU / 4 GB caps. Keep the chunking when touching `transcribe.py` — the `whispercpp-server` backend posts one chunk per request and adds the offset the same way.
- `ollama/ollama:rocm` ships no `rocminfo`; the GPU proof is Ollama's own log line `inference compute … compute=gfx1201`. whisper.cpp proves itself with `ggml_vulkan: Found 1 Vulkan devices` (anything with `llvmpipe` = CPU).
- Ollama truncates prompts above `num_ctx` silently. `llm.py` warns at >0.9·`num_ctx` from a `len/3` estimate and again when `prompt_eval_count` comes back far below it. The refine prompt is the big one (~25k tokens at 30 candidates).
- `vision_triage.json`/`vision.json` store the effective model name (`llm.model_for`), so switching provider rebuilds them; `candidates.json`/`moments.json` do not, so compare providers with separate `TG_DATA_DIR`s.
- The compose project interpolates `.env` from `deploy/local/` (`RENDER_GID`, `VIDEO_GID`, `OLLAMA_*_MODEL` are required); run `bootstrap.sh` rather than `docker compose up` by hand on a fresh box.
- ffmpeg's `ebur128` line format includes a `TARGET:-23 LUFS` field between `t:` and `M:`; the regex in `audio.py` must keep tolerating it (the old one silently matched nothing).
- The app was tuned for Star Citizen content; vision categories and the system prompt in `vision.py` are game-specific.
- README's YouTube section (`YOUTUBE_CLIENT_SECRETS`, `-it` consent run) is stale; the code uses `YOUTUBE_CLIENT_ID/SECRET` + UI OAuth as described above.
- Secrets in `data/settings.json` and `data/oauth.json` are plaintext; `data/` is gitignored.
