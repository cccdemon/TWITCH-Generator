#!/usr/bin/env bash
# Install/update the ClipBox status screen on the attached monitor (tty1).
# Idempotent. Run as root:  sudo /opt/twitch-generator/deploy/local/console/install.sh
# Remove again:             sudo .../install.sh --remove   (tty1 shows the login prompt again)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT=/etc/systemd/system/clipbox-console.service

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now clipbox-console.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload
  systemctl start getty@tty1.service
  echo "status screen removed"
  exit 0
fi

# The dashboard picks the largest Terminus font that fits the monitor (setfont, kbd package).
[ -e /usr/share/consolefonts/Uni2-TerminusBold32x16.psf.gz ] \
  || { export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq console-setup kbd; }

cat > "$UNIT" <<EOF
[Unit]
Description=ClipBox status screen on tty1
After=systemd-vconsole-setup.service docker.service
Conflicts=getty@tty1.service
ConditionPathExists=/dev/tty1

[Service]
ExecStart=/usr/bin/python3 -u $HERE/clipbox-console.py
Environment=CLIPBOX_SETFONT=1
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
Environment=TERM=linux
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable clipbox-console.service >/dev/null 2>&1
systemctl restart clipbox-console.service
sleep 2
systemctl is-active clipbox-console.service
echo "status screen installed on tty1; login: Alt+F2"
