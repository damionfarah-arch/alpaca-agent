"""Read-only web dashboard for the trading agent.

Separate process from agent.py. Reads the same SQLite DB. Never writes.

    .venv/bin/python -m dashboard                 # dev server
    gunicorn -b 0.0.0.0:8080 'dashboard.app:app'  # production (systemd, step 5)
"""

from __future__ import annotations

import functools
import hmac
import sqlite3
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request

import db
from config import CONFIG, ConfigError


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_auth(user: str, pw: str) -> bool:
    return hmac.compare_digest(user or "", CONFIG.dashboard_user) and hmac.compare_digest(
        pw or "", CONFIG.dashboard_password
    )


def _needs_auth() -> Response:
    return Response(
        "authentication required\n",
        401,
        {"WWW-Authenticate": 'Basic realm="alpaca-agent dashboard"'},
    )


def require_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _needs_auth()
        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------- #
def _build_state() -> dict:
    """Everything the page needs, in one payload (single poll)."""
    try:
        conn = db.connect_readonly(CONFIG.db_path)
    except sqlite3.OperationalError:
        return {
            "generated_at": _utcnow_iso(),
            "db_present": False,
            "message": f"database not found at {CONFIG.db_path} -- start the agent first",
            "config": _config_block(),
        }

    try:
        meta = db.get_all_meta(conn)
        account = db.latest_account(conn)
        source = meta.get("account_source") or ("shadow" if CONFIG.is_shadow else "broker")
        first = db.first_account(conn, source) or db.first_account(conn)
        positions = db.latest_positions(conn)
        trades = db.recent_trades(conn, 60)
        decisions = db.recent_decisions(conn, 80)
        events = db.recent_events(conn, 30)
        curve = db.equity_curve(conn, 1500)
        last_loop = db.latest_loop(conn)
        cnt = db.counts(conn)

        held = {p["symbol"] for p in positions}
        # chart every configured symbol; held ones sort first
        syms = sorted(
            CONFIG.all_symbols,
            key=lambda s: (s not in held, CONFIG.all_symbols.index(s)),
        )
        markets = []
        for s in syms:
            ser = db.price_series(conn, s, 240)
            if not ser:
                continue
            last = ser[-1]
            markets.append({
                "symbol": s,
                "held": s in held,
                "asset_class": "crypto" if "/" in s else "us_equity",
                "last_price": last["price"],
                "fast_ma": last["fast_ma"],
                "slow_ma": last["slow_ma"],
                "signal": last["signal"],
                "change_pct": (
                    (ser[-1]["price"] / ser[0]["price"] - 1) * 100
                    if ser[0]["price"] else 0.0
                ),
                "series": [
                    {"ts": r["ts"], "p": r["price"],
                     "f": r["fast_ma"], "s": r["slow_ma"]}
                    for r in ser
                ],
            })
    finally:
        conn.close()

    equity = account["equity"] if account else None
    baseline = first["equity"] if first else None
    all_time_pl = (equity - baseline) if (equity is not None and baseline is not None) else None
    all_time_pl_pct = (
        (all_time_pl / baseline * 100.0)
        if (all_time_pl is not None and baseline)
        else None
    )

    realized = db_safe_realized(trades)

    return {
        "generated_at": _utcnow_iso(),
        "db_present": True,
        "config": _config_block(),
        "status": {
            "agent_state": meta.get("agent_state", "unknown"),
            "execution_mode": meta.get("execution_mode", CONFIG.execution_mode),
            "alpaca_env": meta.get("alpaca_env", CONFIG.alpaca_env),
            "shadow": _truthy(meta.get("shadow")),
            "will_place_real_orders": _truthy(meta.get("will_place_real_orders")),
            "kill_switch": _truthy(meta.get("kill_switch")),
            "strategy": meta.get("strategy", ""),
            "loop_interval_seconds": _int(meta.get("loop_interval_seconds"),
                                          CONFIG.loop_interval_seconds),
            "last_loop_at": meta.get("last_loop_at"),
            "last_loop_finished": meta.get("last_loop_finished"),
            "next_loop_at": meta.get("next_loop_at"),
            "last_loop_status": meta.get("last_loop_status"),
            "halt_reason": meta.get("halt_reason"),
            "equity_market_open": _truthy(meta.get("equity_market_open")),
            "account_source": source,
            "counts": cnt,
            "last_loop": last_loop,
        },
        "account": {
            "equity": equity,
            "cash": account["cash"] if account else None,
            "positions_value": account["positions_value"] if account else None,
            "day_pl": account["day_pl"] if account else None,
            "day_pl_pct": account["day_pl_pct"] if account else None,
            "all_time_pl": all_time_pl,
            "all_time_pl_pct": all_time_pl_pct,
            "realized_pl": account["realized_pl"] if account else None,
            "baseline_equity": baseline,
            "as_of": account["ts"] if account else None,
        },
        "positions": positions,
        "markets": markets,
        "trades": trades,
        "decisions": decisions,
        "events": events,
        "equity_curve": [
            {"ts": r["ts"], "equity": r["equity"], "day_pl": r["day_pl"]}
            for r in curve
        ],
    }


def db_safe_realized(trades: list[dict]) -> float:
    return sum(t["realized_pl"] or 0.0 for t in trades if t.get("realized_pl") is not None)


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _config_block() -> dict:
    return {
        "poll_seconds": CONFIG.dashboard_poll_seconds,
        "base_currency": CONFIG.base_currency,
        "equity_symbols": CONFIG.equity_symbols,
        "crypto_symbols": CONFIG.crypto_symbols,
        "caps": {
            "max_position_notional_usd": CONFIG.risk.max_position_notional_usd,
            "max_total_allocation_usd": CONFIG.risk.max_total_allocation_usd,
            "max_daily_loss_usd": CONFIG.risk.max_daily_loss_usd,
            "max_daily_loss_pct": CONFIG.risk.max_daily_loss_pct,
        },
        "shadow_starting_cash": CONFIG.shadow_starting_cash,
    }


# --------------------------------------------------------------------------- #
def create_app() -> Flask:
    if not CONFIG.dashboard_password:
        raise ConfigError(
            "DASHBOARD_PASSWORD is not set -- the dashboard refuses to start "
            "without HTTP basic auth. Set it in .env."
        )

    app = Flask(__name__)

    @app.get("/")
    @require_auth
    def index():
        return render_template("index.html", poll_seconds=CONFIG.dashboard_poll_seconds)

    @app.get("/api/state")
    @require_auth
    def api_state():
        return jsonify(_build_state())

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "ts": _utcnow_iso()})

    return app


app = create_app()
