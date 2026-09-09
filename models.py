"""Plain data structures shared across the project.

The broker wrapper converts Alpaca SDK objects into these, so strategy / risk /
agent / dashboard code never imports the Alpaca SDK directly. All money is USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    EQUITY = "us_equity"
    CRYPTO = "crypto"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float                 # total portfolio value (cash + positions)
    cash: float
    buying_power: float
    currency: str
    # Alpaca reports the equity as of the last trading-day close; used for
    # intraday P&L math.
    last_equity: float
    raw_status: str
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def positions_value(self) -> float:
        return self.equity - self.cash


@dataclass(frozen=True)
class Position:
    symbol: str                   # normalised: "AAPL", "BTC/USD"
    asset_class: AssetClass
    qty: float                    # signed (negative = short); crypto is long-only
    avg_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_pl_pct: float

    @property
    def side(self) -> Side:
        return Side.BUY if self.qty >= 0 else Side.SELL


@dataclass(frozen=True)
class OrderResult:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: float | None
    notional: float | None
    filled_qty: float
    filled_avg_price: float | None
    status: str
    type: str
    submitted_at: datetime | None
    filled_at: datetime | None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ClockInfo:
    timestamp: datetime
    is_open: bool
    next_open: datetime | None
    next_close: datetime | None
