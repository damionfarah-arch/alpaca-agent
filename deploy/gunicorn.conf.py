"""Gunicorn config for the dashboard. Bind host/port come from the same .env
as everything else (loaded via config.CONFIG)."""

from config import CONFIG

bind = f"{CONFIG.dashboard_host}:{CONFIG.dashboard_port}"
workers = 2
threads = 4
timeout = 30
graceful_timeout = 20
keepalive = 5

accesslog = "-"          # -> journald via systemd
errorlog = "-"
_lvl = CONFIG.log_level.lower()
loglevel = _lvl if _lvl in {"debug", "info", "warning", "error", "critical"} else "info"
proc_name = "alpaca-dashboard"
