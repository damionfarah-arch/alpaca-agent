#!/usr/bin/env bash
# =============================================================================
# One-shot server setup for the Alpaca agent + dashboard.
# Target: fresh Ubuntu 22.04 / 24.04 LTS.  Run as root:
#
#     bash setup.sh git@github.com:YOURNAME/alpaca-agent.git
#
# Safe to re-run (idempotent) -- use it for updates too.
# =============================================================================
set -euo pipefail

REPO_URL="${1:-}"
APP_USER="alpaca"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/alpaca-agent"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mXX  %s\033[0m\n' "$*" >&2; exit 1; }
# run a command as the app user, with its own HOME
as_app() { sudo -u "$APP_USER" -H "$@"; }

[ "$(id -u)" -eq 0 ] || die "run as root (use: sudo bash setup.sh <repo-url>)"
[ -n "$REPO_URL" ] || die "usage: bash setup.sh git@github.com:YOURNAME/alpaca-agent.git"

# --- 1. system packages ----------------------------------------------------
say "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ufw curl >/dev/null
echo "ok"

# --- 2. app user ---------------------------------------------------------
if ! id "$APP_USER" &>/dev/null; then
  say "creating user '${APP_USER}'"
  adduser --system --group --home "$APP_HOME" --shell /bin/bash "$APP_USER"
fi
mkdir -p "${APP_HOME}/.ssh" "${APP_DIR}/data"
chmod 700 "${APP_HOME}/.ssh"
chown -R "${APP_USER}:${APP_USER}" "$APP_HOME"

# --- 3. deploy key (read-only GitHub access) ---------------------------
KEY="${APP_HOME}/.ssh/id_ed25519"
if [ ! -f "$KEY" ]; then
  say "generating a GitHub deploy key"
  as_app ssh-keygen -t ed25519 -N "" -f "$KEY" -C "alpaca-agent-deploy" >/dev/null
fi
as_app bash -c "ssh-keyscan -t ed25519,rsa github.com 2>/dev/null >> ${APP_HOME}/.ssh/known_hosts" || true
if [ -f "${APP_HOME}/.ssh/known_hosts" ]; then
  sort -u "${APP_HOME}/.ssh/known_hosts" -o "${APP_HOME}/.ssh/known_hosts"
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}/.ssh"

if ! as_app git ls-remote "$REPO_URL" &>/dev/null; then
  echo
  warn "GitHub can't be reached with this deploy key yet."
  echo   "  1. Copy the line below."
  echo   "  2. GitHub -> your repo -> Settings -> Deploy keys -> Add deploy key"
  echo   "     Title: 'vps'   Key: (paste)   Allow write access: NO"
  echo   "  3. Re-run this script."
  echo
  printf '\033[1;32m%s\033[0m\n\n' "$(cat "${KEY}.pub")"
  exit 1
fi
echo "deploy key works"

# --- 4. clone / update -------------------------------------------------
if [ -d "${APP_DIR}/.git" ]; then
  say "updating existing checkout"
  as_app git -C "$APP_DIR" fetch --quiet origin
  BR="$(as_app git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)"
  as_app git -C "$APP_DIR" reset --hard "origin/${BR}"
else
  say "cloning repo"
  as_app git clone --quiet "$REPO_URL" "$APP_DIR"
fi
mkdir -p "${APP_DIR}/data"
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"

# --- 5. python venv + deps -----------------------------------------
say "building virtualenv"
if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
  as_app python3 -m venv "${APP_DIR}/.venv"
fi
as_app "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
as_app "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
echo "ok"

# --- 6. .env ---------------------------------------------------------
ENV_NEEDS_EDIT=0
if [ ! -f "${APP_DIR}/.env" ]; then
  say "creating .env from template"
  as_app cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  ENV_NEEDS_EDIT=1
fi
chmod 600 "${APP_DIR}/.env"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"

# --- 7. systemd units ---------------------------------------------
say "installing systemd services"
install -m 644 "${APP_DIR}/deploy/alpaca-agent.service"     /etc/systemd/system/
install -m 644 "${APP_DIR}/deploy/alpaca-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable alpaca-agent.service alpaca-dashboard.service >/dev/null 2>&1
echo "ok (enabled on boot)"

# --- 8. firewall -------------------------------------------------
say "configuring firewall (ufw)"
DASH_PORT="$(as_app bash -c "cd '${APP_DIR}' && .venv/bin/python -c 'from config import CONFIG; print(CONFIG.dashboard_port)'" 2>/dev/null || echo 8080)"
ufw allow OpenSSH >/dev/null
ufw allow "${DASH_PORT}/tcp" >/dev/null
ufw --force enable >/dev/null
echo "ok (SSH + ${DASH_PORT}/tcp open)"

# --- 9. done ---------------------------------------------------
echo
if [ "$ENV_NEEDS_EDIT" -eq 1 ]; then
  say "NEXT: edit the config, then start the services"
  cat <<EOF

  nano ${APP_DIR}/.env
     - ALPACA_API_KEY / ALPACA_SECRET_KEY   (paper keys)
     - confirm  ALPACA_ENV=paper  and  EXECUTION_MODE=shadow
     - set a long unique  DASHBOARD_PASSWORD

  systemctl start alpaca-agent alpaca-dashboard
  systemctl status alpaca-agent alpaca-dashboard
EOF
else
  say "restarting services"
  systemctl restart alpaca-agent alpaca-dashboard
  sleep 2
  systemctl is-active alpaca-agent alpaca-dashboard || true
  IP="$(curl -s4 --max-time 5 ifconfig.co 2>/dev/null || echo YOUR_SERVER_IP)"
  echo
  echo "  dashboard:  http://${IP}:${DASH_PORT}"
  echo "  agent log:  journalctl -u alpaca-agent -f"
fi
