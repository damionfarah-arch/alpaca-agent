"""Dev server entrypoint:  .venv/bin/python -m dashboard

Production uses gunicorn against dashboard.app:app (see deploy/, step 5).
"""

from __future__ import annotations

import logging

from config import CONFIG
from dashboard.app import app

if __name__ == "__main__":
    logging.basicConfig(level=CONFIG.log_level)
    print(
        f"dashboard -> http://{CONFIG.dashboard_host}:{CONFIG.dashboard_port}  "
        f"(user: {CONFIG.dashboard_user}, poll: {CONFIG.dashboard_poll_seconds}s, "
        f"db: {CONFIG.db_path})"
    )
    app.run(
        host=CONFIG.dashboard_host,
        port=CONFIG.dashboard_port,
        debug=False,
        threaded=True,
    )
