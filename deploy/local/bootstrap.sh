#!/usr/bin/env bash
# ClipBox bootstrap — idempotent. Runs as root from clipbox-bootstrap.service on
# the first boot after the USB install, and can be re-run by hand any time:
#
#   sudo /opt/twitch-generator/deploy/local/bootstrap.sh
#
# Steps: .env (secrets, GIDs, LAN URL, model names from config.yaml) → Docker log
# rotation → compose up --build → wait for model pull → GPU proof (Ollama sees
# gfx1201, whisper sees a Vulkan AMD device) → UI reachable → /etc/motd.
# On success the systemd unit disables itself; on failure it stays enabled so the
# next boot retries, and /etc/motd says what went wrong.
set -uo pipefail

REPO_DIR="${CLIPBOX_REPO_DIR:-/opt/twitch-generator}"
LOCAL_DIR="$REPO_DIR/deploy/local"
COMPOSE=(docker compose -f "$LOCAL_DIR/docker-compose.local.yml" --project-directory "$LOCAL_DIR")
ENV_FILE="$LOCAL_DIR/.env"
CFG="$LOCAL_DIR/config.yaml"
FIRST_LOGIN="$REPO_DIR/FIRST-LOGIN.txt"
MOTD=/etc/motd
UNIT=clipbox-bootstrap.service
GPU_TARGET="${CLIPBOX_GPU_TARGET:-gfx1201}"
PULL_TIMEOUT_MIN="${CLIPBOX_PULL_TIMEOUT_MIN:-120}"

log() { printf '[bootstrap] %s\n' "$*"; }

fail() {
  local msg="$1"
  log "FAILED: $msg"
  cat >"$MOTD" <<EOF

  ClipBox bootstrap FAILED: $msg
  Logs:   journalctl -u $UNIT -b     |  docker ps -a
          docker logs clipbox-ollama | docker logs clipbox-whisper
  Retry:  sudo $LOCAL_DIR/bootstrap.sh   (also runs again on next boot)

EOF
  exit 1
}

# ------------------------------------------------------------- preconditions --
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -f "$CFG" ]] || fail "config not found: $CFG"
command -v docker >/dev/null || fail "docker not installed"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin missing (apt install docker-compose-v2)"
[[ -e /dev/kfd && -e /dev/dri ]] || fail "/dev/kfd or /dev/dri missing — amdgpu driver not loaded (kernel too old for RDNA4?)"

# -------------------------------------------------------- helpers: .env edits --
set_env() {  # set_env KEY VALUE  — replace or append, keep file otherwise untouched
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    local cur
    cur=$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)
    [[ "$cur" == "$val" ]] && return 0
    sed -i -E "s|^${key}=.*|${key}=${val//|/\\|}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >>"$ENV_FILE"
  fi
  log ".env: $key updated"
}

cfg_value() {  # cfg_value KEY — first "key: value" line in config.yaml, quotes stripped
  sed -n -E "s/^[[:space:]]*$1:[[:space:]]*\"?([^\"#]+)\"?.*$/\1/p" "$CFG" | head -1 | xargs
}

# ------------------------------------------------------------- 1. .env --------
if [[ ! -f "$ENV_FILE" ]]; then
  log "creating .env from .env.example"
  cp "$LOCAL_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
  set_env USERS "clip:${PASSWORD}"
  set_env SESSION_SECRET "$(tr -dc 'a-f0-9' </dev/urandom | head -c 64)"
  umask 077
  cat >"$FIRST_LOGIN" <<EOF
ClipBox web login (generated $(date -Is))
  user:     clip
  password: ${PASSWORD}
Change it in $ENV_FILE (USERS=) and restart: docker compose -f $LOCAL_DIR/docker-compose.local.yml up -d
EOF
  chown "$(stat -c %U "$REPO_DIR")" "$FIRST_LOGIN" 2>/dev/null || true
  umask 022
fi

# 2. GIDs of render/video for the GPU containers
RENDER_GID=$(getent group render | cut -d: -f3)
VIDEO_GID=$(getent group video | cut -d: -f3)
[[ -n "$RENDER_GID" && -n "$VIDEO_GID" ]] || fail "groups render/video not found"
set_env RENDER_GID "$RENDER_GID"
set_env VIDEO_GID "$VIDEO_GID"

# 3. PUBLIC_URL from the first non-loopback IPv4
IP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -1)
[[ -n "$IP" ]] || IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)
[[ -n "$IP" ]] || fail "no IPv4 address yet (network not up?)"
PORT=$(grep -E '^WEB_PORT=' "$ENV_FILE" | cut -d= -f2); PORT=${PORT:-9443}
set_env PUBLIC_URL "http://${IP}:${PORT}"

# 3b. model names: config.yaml is the source of truth for Ollama, .env for whisper
TEXT_MODEL=$(cfg_value text_model); VISION_MODEL=$(cfg_value vision_model)
[[ -n "$TEXT_MODEL" && -n "$VISION_MODEL" ]] || fail "llm.ollama.text_model/vision_model missing in $CFG"
set_env OLLAMA_TEXT_MODEL "$TEXT_MODEL"
set_env OLLAMA_VISION_MODEL "$VISION_MODEL"
WLANG=$(awk '/^whisper:/{f=1} f&&/^[^ ]/&&!/^whisper:/{f=0} f&&/^[[:space:]]+language:/{print $2; exit}' "$CFG")
[[ -n "$WLANG" && "$WLANG" != "null" ]] && set_env WHISPER_LANG "$WLANG"

# ------------------------------------------------- 4. docker log rotation -----
DAEMON_JSON=/etc/docker/daemon.json
WANT='{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}'
if [[ ! -f "$DAEMON_JSON" ]]; then
  log "setting Docker log rotation (json-file 10m x 3)"
  mkdir -p /etc/docker; printf '%s\n' "$WANT" >"$DAEMON_JSON"
  systemctl restart docker
elif ! grep -q '"max-size"' "$DAEMON_JSON"; then
  log "WARNING: $DAEMON_JSON exists without log-opts — not touching it; add max-size/max-file yourself"
fi

# ------------------------------------------------------ 5. compose up ---------
log "docker compose up -d --build (first run builds the app image and pulls ollama/whisper images)"
if ! "${COMPOSE[@]}" up -d --build --remove-orphans; then
  # depends_on conditions make `up` wait for health/completion; a long model
  # pull can exceed compose's own patience. Fall through to our own wait loop.
  log "compose up returned non-zero (probably still pulling) — waiting on ollama-init"
fi

# 6. wait for ollama-init (model pull) to exit 0
log "waiting for model pull (ollama-init), timeout ${PULL_TIMEOUT_MIN} min"
deadline=$(( $(date +%s) + PULL_TIMEOUT_MIN * 60 ))
while :; do
  state=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' clipbox-ollama-init 2>/dev/null || echo "missing 1")
  case "$state" in
    "exited 0") log "models present"; break ;;
    exited*)    fail "ollama-init exited with ${state#exited }: docker logs clipbox-ollama-init" ;;
  esac
  (( $(date +%s) > deadline )) && fail "model pull did not finish within ${PULL_TIMEOUT_MIN} min"
  sleep 15
done
# ollama-init done → app may now start if compose gave up earlier
"${COMPOSE[@]}" up -d --no-build >/dev/null 2>&1 || true

# ------------------------------------------------------ 7. GPU proof ----------
# The ROCm image ships no rocminfo; Ollama's own discovery line is the proof:
#   inference compute ... library=rocm ... compute=gfx1201 ... name="AMD Radeon RX 9070 XT"
if docker logs clipbox-ollama 2>&1 | grep -i 'inference compute' | grep -qi "$GPU_TARGET"; then
  OLLAMA_GPU="ok ($(docker logs clipbox-ollama 2>&1 | grep -i 'inference compute' | grep -i "$GPU_TARGET" | tail -1 | grep -oE 'name="[^"]+"|total="[^"]+"' | tr '\n' ' '))"
else
  fail "Ollama does not see the GPU as $GPU_TARGET — check: docker logs clipbox-ollama | grep -i 'inference compute'; dmesg | grep -i amdgpu"
fi

# whisper.cpp Vulkan: must list an AMD device, not llvmpipe (CPU software rasteriser)
for _ in $(seq 1 60); do
  WLOG=$(docker logs clipbox-whisper 2>&1 | grep -i 'ggml_vulkan' | head -3)
  [[ -n "$WLOG" ]] && break
  sleep 10
done
if echo "$WLOG" | grep -qi 'llvmpipe'; then
  fail "whisper.cpp runs on llvmpipe (CPU) — Mesa in the image too old for RDNA4 or /dev/dri not usable"
elif echo "$WLOG" | grep -qiE 'radeon|amd|navi|gfx'; then
  WHISPER_GPU="ok ($(echo "$WLOG" | grep -oiE '(AMD|Radeon)[^|]*' | head -1 | xargs))"
else
  fail "whisper.cpp reports no Vulkan device yet — docker logs clipbox-whisper"
fi

# ------------------------------------------------------ 8. UI reachable -------
log "waiting for the web UI"
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 5
done
curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || fail "web UI not answering on :${PORT} — docker logs clipbox-app"

# ------------------------------------------------------ 9. motd + disable -----
cat >"$MOTD" <<EOF

  ClipBox is up.   Web UI:  http://${IP}:${PORT}
  First login:     user clip — password in ${FIRST_LOGIN}
  GPU:             ollama ${OLLAMA_GPU}
                   whisper ${WHISPER_GPU}
  Models:          ${TEXT_MODEL} (text), ${VISION_MODEL} (vision), whisper $(grep -E '^WHISPER_MODEL=' "$ENV_FILE" | cut -d= -f2)
  Manage:          cd $LOCAL_DIR && docker compose -f docker-compose.local.yml {ps,logs -f,restart,down}
  Docs:            $LOCAL_DIR/README.md

EOF
log "done — UI at http://${IP}:${PORT}"
if systemctl is-enabled "$UNIT" >/dev/null 2>&1; then
  systemctl disable "$UNIT" >/dev/null 2>&1 && log "disabled $UNIT (containers restart on their own via Docker)"
fi
exit 0
