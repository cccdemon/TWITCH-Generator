#!/usr/bin/env bash
# Enable the ClipBox portal: one LAN hostname with a real certificate in front
# of every UI on the box. Idempotent. Run as root:
#
#   sudo bash /opt/twitch-generator/deploy/local/proxy/enable.sh box.example.org
#
# Before running:
#   1. DNS A record  <domain> -> LAN IP of this box  (Cloudflare, DNS only).
#      FRITZ!Box: add the name under Heimnetz > Netzwerk > Netzwerkeinstellungen >
#      DNS-Rebind-Schutz, otherwise the router drops answers with private IPs.
#   2. CF_API_TOKEN=<token with Zone:DNS:Edit for the zone> in deploy/local/.env
#      (Caddy solves the ACME DNS-01 challenge with it; nothing is exposed).
#
# What it does: .env (BOX_DOMAIN, COMPOSE_PROFILES=proxy, ROOT_PATH=/vod,
# PUBLIC_URL), /etc/cockpit/cockpit.conf (UrlRoot=/system, allowed origins),
# builds + starts the proxy container, recreates the app (skipped while a job
# is running: recreating the app kills it; re-run this script afterwards).
set -euo pipefail

DOMAIN="${1:-}"
[[ -n "$DOMAIN" ]] || { echo "usage: $0 <domain>" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 2; }

LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$LOCAL_DIR/../.." && pwd)"
ENV_FILE="$LOCAL_DIR/.env"
COMPOSE=(docker compose -f "$LOCAL_DIR/docker-compose.local.yml" --project-directory "$LOCAL_DIR")
log() { printf '[proxy] %s\n' "$*"; }

[[ -f "$ENV_FILE" ]] || { echo "$ENV_FILE missing: run bootstrap.sh first" >&2; exit 1; }
grep -qE '^CF_API_TOKEN=.+' "$ENV_FILE" || { echo "CF_API_TOKEN missing in $ENV_FILE" >&2; exit 1; }

set_env() {  # set_env KEY VALUE: replace or append, keeps the rest of .env
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i -E "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >>"$ENV_FILE"
  fi
}
set_env BOX_DOMAIN "$DOMAIN"
set_env COMPOSE_PROFILES proxy
set_env ROOT_PATH /vod
set_env PUBLIC_URL "https://${DOMAIN}/vod"

# Cockpit behind the proxy: served under /system, accepts the proxy's origin.
IP=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -1 || true)
CONF=/etc/cockpit/cockpit.conf
WANT="[WebService]
Origins = https://${DOMAIN} wss://${DOMAIN}${IP:+ https://${IP}:9090 wss://${IP}:9090}
ProtocolHeader = X-Forwarded-Proto
ForwardedForHeader = X-Forwarded-For
UrlRoot = /system"
if [[ "$(cat "$CONF" 2>/dev/null || true)" != "$WANT" ]]; then
  [[ -f "$CONF" ]] && cp "$CONF" "$CONF.bak.$(date +%s)"
  mkdir -p /etc/cockpit
  printf '%s\n' "$WANT" >"$CONF"
  systemctl restart cockpit.socket
  systemctl try-restart cockpit.service || true
  log "cockpit.conf written (direct access is now https://${IP:-<ip>}:9090/system/)"
fi

log "building and starting the proxy"
"${COMPOSE[@]}" up -d --build --no-deps proxy

JOBS="$REPO_DIR/data/jobs.json"
ACTIVE=0
if [[ -f "$JOBS" ]]; then
  ACTIVE=$(grep -oE '"status": *"(running|queued)"' "$JOBS" | wc -l || true)
fi
if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' clipbox-app 2>/dev/null | grep -qx 'ROOT_PATH=/vod'; then
  log "app already runs with ROOT_PATH=/vod"
elif [[ "$ACTIVE" != "0" ]]; then
  log "NOT recreating the app: $ACTIVE job(s) running/queued. /vod/ stays broken until you re-run: sudo bash $0 $DOMAIN"
else
  log "recreating the app with ROOT_PATH=/vod"
  "${COMPOSE[@]}" up -d --no-build --no-deps app
fi
log "done: https://${DOMAIN}/"
