#!/bin/sh
# Install/update clip-poster on the phone host (run as root from /opt/clip-poster).
# Idempotent: keeps .env and data/.
set -eu
cd /opt/clip-poster
command -v adb >/dev/null || { echo "adb fehlt: apt install adb"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 && python3 -c "import ensurepip" 2>/dev/null \
  || apt-get install -y python3-venv
id clipposter >/dev/null 2>&1 || useradd --system --home-dir /opt/clip-poster --shell /usr/sbin/nologin clipposter
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  sed "s/^POSTER_TOKEN=.*/POSTER_TOKEN=$TOKEN/" .env.example > .env
fi
chown root:clipposter .env && chmod 640 .env
mkdir -p data && chown -R clipposter:clipposter data
cp adb-server.service clip-poster.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now adb-server.service
systemctl enable clip-poster.service
systemctl restart clip-poster.service
sleep 2
systemctl --no-pager --lines=5 status clip-poster.service || true
