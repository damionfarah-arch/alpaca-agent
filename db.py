"""SQLite persistence -- the shared source of truth between agent.py and the
dashboard. WAL mode so the dashboard can read while the agent writes.

Timestamps are stored as ISO-8601 UTC strings (sortable, unambiguous).
All money is USD.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models import AccountSnapshot, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS loops (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL,          -- running | ok | halted | error
    halt_reason        TEXT,
    equity_market_open INTEGER,
    execution_mode     TEXT NOT NULL,
    alpaca_env         TEXT NOT NULL,
    error              TEXT
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    loop_id         INTEGER,
    source          TEXT NOT NULL DEFAULT 'broker',  -- 'broker' | 'shadow'
    equity          REAL NOT NULL,
    cash            REAL NOT NULL,
    buying_power    REAL NOT NULL,
    positions_value REAL NOT NULL,
    realized_pl     REAL NOT NULL DEFAULT 0,
    day_open_equity REAL,
    day_pl          REAL,
    day_pl_pct      REAL
);
CREATE INDEX IF NOT EXISTS idx_account_ts ON account_snapshots(ts);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    loop_id           INTEGER,
    source            TEXT NOT NULL DEFAULT 'broker',  -- 'broker' | 'shadow'
    symbol            TEXT NOT NULL,
    asset_class       TEXT NOT NULL,
    qty               REAL NOT NULL,
    avg_entry_price   REAL NOT NULL,
    current_price     REAL NOT NULL,
    market_value      REAL NOT NULL,
    cost_basis        REAL NOT NULL,
    unrealized_pl     REAL NOT NULL,
    unrealized_pl_pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_possnap_ts ON position_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_possnap_loop ON position_snapshots(loop_id);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    loop_id         INTEGER,
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    signal          TEXT NOT NULL,   -- raw strategy: buy | sell | hold
    intent          TEXT NOT NULL,   -- after position logic: buy | sell | hold
    executed        INTEGER NOT NULL,-- 1 if an order was placed (paper or live)
    shadow          INTEGER NOT NULL,-- 1 if shadow mode (no order placed)
    blocked         INTEGER NOT NULL,-- 1 if a risk rail blocked the intent
    price           REAL,
    fast_ma         REAL,
    slow_ma         REAL,
    reason          TEXT NOT NULL,   -- human-readable strategy reasoning
    position_reason TEXT,            -- why intent differs from signal
    risk_allowed    INTEGER,
    risk_reason     TEXT,
    order_id        TEXT,
    indicators      TEXT             -- JSON
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    loop_id      INTEGER,
    decision_id  INTEGER,
    symbol       TEXT NOT NULL,
    asset_class  TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL,
    notional     REAL,
    ref_price    REAL,               -- price at decision time
    fill_price   REAL,               -- sim fill (shadow) or actual fill (live)
    realized_pl  REAL,               -- on a sell that closes a position
    status       TEXT NOT NULL,      -- shadow | submitted | filled | rejected | error
    shadow       INTEGER NOT NULL,
    order_id     TEXT,
    reason       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS shadow_positions (
    symbol          TEXT PRIMARY KEY,
    asset_class     TEXT NOT NULL,
    qty             REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    cost_basis      REAL NOT NULL,
    opened_at       TEXT NOT NULL,
    opened_loop_id  INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    loop_id INTEGER,
    kind    TEXT NOT NULL,           -- start|stop|kill_switch|daily_loss_halt|resume|error|info
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if read_only:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def start_loop(conn: sqlite3.Connection, execution_mode: str, alpaca_env: str) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO loops (started_at, status, execution_mode, alpaca_env) "
            "VALUES (?, 'running', ?, ?)",
            (_now(), execution_mode, alpaca_env),
        )
    return int(cur.lastrowid)


def finish_loop(
    conn: sqlite3.Connection,
    loop_id: int,
    status: str,
    *,
    halt_reason: str | None = None,
    equity_market_open: bool | None = None,
    error: str | None = None,
) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE loops SET finished_at=?, status=?, halt_reason=?, "
            "equity_market_open=?, error=? WHERE id=?",
            (
                _now(),
                status,
                halt_reason,
                None if equity_market_open is None else int(equity_market_open),
                error,
                loop_id,
            ),
        )


def record_account(
    conn: sqlite3.Connection,
    loop_id: int,
    acct: AccountSnapshot,
    *,
    source: str = "broker",
    realized_pl: float = 0.0,
    day_open_equity: float | None,
    day_pl: float | None,
    day_pl_pct: float | None,
) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO account_snapshots (ts, loop_id, source, equity, cash, "
            "buying_power, positions_value, realized_pl, day_open_equity, day_pl, "
            "day_pl_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                loop_id,
                source,
                acct.equity,
                acct.cash,
                acct.buying_power,
                acct.positions_value,
                realized_pl,
                day_open_equity,
                day_pl,
                day_pl_pct,
            ),
        )


def record_positions(
    conn: sqlite3.Connection,
    loop_id: int,
    positions: list[Position],
    *,
    source: str = "broker",
) -> None:
    ts = _now()
    with tx(conn):
        for p in positions:
            conn.execute(
                "INSERT INTO position_snapshots (ts, loop_id, source, symbol, "
                "asset_class, qty, avg_entry_price, current_price, market_value, "
                "cost_basis, unrealized_pl, unrealized_pl_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    loop_id,
                    source,
                    p.symbol,
                    p.asset_class.value,
                    p.qty,
                    p.avg_entry_price,
                    p.current_price,
                    p.market_value,
                    p.cost_basis,
                    p.unrealized_pl,
                    p.unrealized_pl_pct,
                ),
            )


def record_decision(
    conn: sqlite3.Connection,
    *,
    loop_id: int,
    symbol: str,
    asset_class: str,
    signal: str,
    intent: str,
    executed: bool,
    shadow: bool,
    blocked: bool,
    price: float | None,
    fast_ma: float | None,
    slow_ma: float | None,
    reason: str,
    position_reason: str | None,
    risk_allowed: bool | None,
    risk_reason: str | None,
    order_id: str | None,
    indicators: dict[str, Any] | None,
) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO decisions (ts, loop_id, symbol, asset_class, signal, intent, "
            "executed, shadow, blocked, price, fast_ma, slow_ma, reason, "
            "position_reason, risk_allowed, risk_reason, order_id, indicators) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                loop_id,
                symbol,
                asset_class,
                signal,
                intent,
                int(executed),
                int(shadow),
                int(blocked),
                price,
                fast_ma,
                slow_ma,
                reason,
                position_reason,
                None if risk_allowed is None else int(risk_allowed),
                risk_reason,
                order_id,
                json.dumps(indicators) if indicators is not None else None,
            ),
        )
    return int(cur.lastrowid)


def record_trade(
    conn: sqlite3.Connection,
    *,
    loop_id: int,
    decision_id: int | None,
    symbol: str,
    asset_class: str,
    side: str,
    qty: float | None,
    notional: float | None,
    ref_price: float | None,
    fill_price: float | None,
    status: str,
    shadow: bool,
    order_id: str | None,
    reason: str,
    realized_pl: float | None = None,
) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO trades (ts, loop_id, decision_id, symbol, asset_class, side, "
            "qty, notional, ref_price, fill_price, realized_pl, status, shadow, "
            "order_id, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                loop_id,
                decision_id,
                symbol,
                asset_class,
                side,
                qty,
                notional,
                ref_price,
                fill_price,
                realized_pl,
                status,
                int(shadow),
                order_id,
                reason,
            ),
        )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# shadow portfolio
# --------------------------------------------------------------------------- #
def get_shadow_positions(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM shadow_positions ORDER BY symbol"
        ).fetchall()
    ]


def open_shadow_position(
    conn: sqlite3.Connection,
    *,
    loop_id: int,
    symbol: str,
    asset_class: str,
    qty: float,
    price: float,
) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO shadow_positions (symbol, asset_class, qty, avg_entry_price, "
            "cost_basis, opened_at, opened_loop_id) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET qty=qty+excluded.qty, "
            "cost_basis=cost_basis+excluded.cost_basis, "
            "avg_entry_price=(cost_basis+excluded.cost_basis)/(qty+excluded.qty)",
            (symbol, asset_class, qty, price, qty * price, _now(), loop_id),
        )


def close_shadow_position(conn: sqlite3.Connection, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shadow_positions WHERE symbol=?", (symbol,)
    ).fetchone()
    if not row:
        return None
    with tx(conn):
        conn.execute("DELETE FROM shadow_positions WHERE symbol=?", (symbol,))
    return dict(row)


def shadow_realized_pl(conn: sqlite3.Connection, since: str | None = None) -> float:
    if since:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pl),0) AS s FROM trades "
            "WHERE shadow=1 AND realized_pl IS NOT NULL AND ts >= ?",
            (since,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pl),0) AS s FROM trades "
            "WHERE shadow=1 AND realized_pl IS NOT NULL"
        ).fetchone()
    return float(row["s"])


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    loop_id: int | None = None,
) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO events (ts, loop_id, kind, message) VALUES (?,?,?,?)",
            (_now(), loop_id, kind, message),
        )


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(value), _now()),
        )


def set_meta_many(conn: sqlite3.Connection, items: dict[str, Any]) -> None:
    with tx(conn):
        for key, value in items.items():
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, str(value), _now()),
            )


# --------------------------------------------------------------------------- #
# reads (used by agent for day math + by the dashboard)
# --------------------------------------------------------------------------- #
def day_open_equity(
    conn: sqlite3.Connection,
    day: str | None = None,
    *,
    source: str = "broker",
) -> float | None:
    day = day or utc_day()
    row = conn.execute(
        "SELECT equity FROM account_snapshots WHERE ts >= ? AND source = ? "
        "ORDER BY ts ASC LIMIT 1",
        (day, source),
    ).fetchone()
    return float(row["equity"]) if row else None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_all_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM meta").fetchall()
    }


def recent_decisions(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def recent_trades(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def latest_account(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def latest_positions(conn: sqlite3.Connection) -> list[dict]:
    """Positions from the most recent loop that recorded any."""
    row = conn.execute(
        "SELECT loop_id FROM position_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return []
    rows = conn.execute(
        "SELECT * FROM position_snapshots WHERE loop_id=? ORDER BY symbol", (row["loop_id"],)
    ).fetchall()
    return [dict(r) for r in rows]


def equity_curve(conn: sqlite3.Connection, limit: int = 2000) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, equity, cash, day_pl FROM account_snapshots "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_loop(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM loops ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
