"""Simulated portfolio for shadow mode.

No orders are placed. When the strategy + risk rails would have bought/sold, the
agent calls `open()` / `close()` here instead, and the position book + a
synthetic account equity are persisted to the same DB the dashboard reads.

Fills are simulated at the price passed in (the latest quote at decision time).
Long-only, one open lot per symbol, full-position exits -- matching the agent's
current trading logic.
"""

from __future__ import annotations

import logging
import sqlite3

import db
from config import Config
from models import AccountSnapshot, AssetClass, Position, utcnow

log = logging.getLogger("shadow")


class ShadowPortfolio:
    def __init__(self, conn: sqlite3.Connection, config: Config):
        self.conn = conn
        self.cfg = config

    # ------------------------------------------------------------------ #
    def positions(self, prices: dict[str, float]) -> list[Position]:
        out: list[Position] = []
        for row in db.get_shadow_positions(self.conn):
            sym = row["symbol"]
            px = prices.get(sym) or row["avg_entry_price"]
            qty = row["qty"]
            mv = qty * px
            cost = row["cost_basis"]
            upl = mv - cost
            out.append(
                Position(
                    symbol=sym,
                    asset_class=AssetClass(row["asset_class"]),
                    qty=qty,
                    avg_entry_price=row["avg_entry_price"],
                    current_price=px,
                    market_value=mv,
                    cost_basis=cost,
                    unrealized_pl=upl,
                    unrealized_pl_pct=(upl / cost * 100.0) if cost else 0.0,
                )
            )
        return out

    def account(self, prices: dict[str, float]) -> AccountSnapshot:
        positions = self.positions(prices)
        realized = db.shadow_realized_pl(self.conn)
        invested_cost = sum(p.cost_basis for p in positions)
        positions_value = sum(p.market_value for p in positions)
        cash = self.cfg.shadow_starting_cash + realized - invested_cost
        equity = cash + positions_value
        return AccountSnapshot(
            equity=equity,
            cash=cash,
            buying_power=cash,           # no margin in the sim
            currency="USD",
            last_equity=equity,
            raw_status="SHADOW",
        )

    # ------------------------------------------------------------------ #
    def has_position(self, symbol: str) -> bool:
        return any(
            r["symbol"] == symbol for r in db.get_shadow_positions(self.conn)
        )

    def open(
        self, *, loop_id: int, symbol: str, asset_class: AssetClass,
        notional: float, price: float,
    ) -> float:
        qty = notional / price if price > 0 else 0.0
        db.open_shadow_position(
            self.conn, loop_id=loop_id, symbol=symbol,
            asset_class=asset_class.value, qty=qty, price=price,
        )
        log.info("shadow OPEN %s qty=%.8g @ $%.2f (~$%.2f)", symbol, qty, price, notional)
        return qty

    def close(self, *, symbol: str, price: float) -> tuple[float, float]:
        """Returns (qty_closed, realized_pl)."""
        row = db.close_shadow_position(self.conn, symbol)
        if not row:
            return 0.0, 0.0
        qty = row["qty"]
        realized = qty * price - row["cost_basis"]
        log.info(
            "shadow CLOSE %s qty=%.8g @ $%.2f -> realized $%.2f",
            symbol, qty, price, realized,
        )
        return qty, realized
