#!/usr/bin/env bash
# Push local code to the VPS and restart the services. Run from your Mac.
#
#     bash deploy/push.sh root@SERVER_IP
#     DEPLOY_HOST=root@SERVER_IP bash deploy/push.sh
#
# Does NOT copy .env or data/ -- those live only on the server.
set -euo pipefail

HOST="${1:-${DEPLOY_HOST:-}}"
[ -n "$HOST" ] || { echo "usage: bash deploy/push.sh root@SERVER_IP" >&2; exit 1; }

REMOTE_DIR="/home/alpaca/alpaca-agent"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> syncing code to ${HOST}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.venv' --exclude 'data' --exclude '.git' --exclude '.env' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.claude' \
  "${HERE}/" "${HOST}:${REMOTE_DIR}/"

echo "==> reinstalling deps, units, and restarting"
ssh "$HOST" bash -s <<'REMOTE'
set -e
cd /home/alpaca/alpaca-agent
chown -R alpaca:alpaca /home/alpaca/alpaca-agent
sudo -u alpaca -H .venv/bin/pip install -q -r requirements.txt
install -m644 deploy/alpaca-agent.service     /etc/systemd/system/
install -m644 deploy/alpaca-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart alpaca-agent alpaca-dashboard
sleep 2
systemctl is-active alpaca-agent alpaca-dashboard
REMOTE

echo "==> done"
