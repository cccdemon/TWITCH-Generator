#!/usr/bin/env bash
# Install/update the ClipBox Cockpit plugin (GPU & Sensoren) and enable PCP history.
# Idempotent. Run as root:  sudo /opt/twitch-generator/deploy/local/cockpit/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST=/usr/share/cockpit/clipbox-gpu

if ! dpkg -s cockpit-ws >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq -t noble-backports cockpit cockpit-storaged cockpit-packagekit cockpit-files
  apt-get install -y -qq cockpit-pcp lm-sensors
  systemctl enable --now cockpit.socket
fi
systemctl enable --now pmcd pmlogger >/dev/null 2>&1 || true

mkdir -p "$DEST"
cp "$HERE/clipbox-gpu/manifest.json" "$HERE/clipbox-gpu/index.html" "$DEST/"
chmod 644 "$DEST"/*
echo "cockpit plugin installed: $DEST (reload the Cockpit page: menu entry 'GPU & Sensoren')"
