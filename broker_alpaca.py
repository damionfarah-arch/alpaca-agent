"""Thin wrapper around the Alpaca SDK (`alpaca-py`).

Responsibilities:
  * one place that knows about paper vs live endpoints
  * convert Alpaca SDK objects -> project `models` dataclasses
  * normalise crypto symbols to slash form ("BTCUSD" -> "BTC/USD")
  * refuse to submit orders while EXECUTION_MODE=shadow (defense in depth;
    the agent already never calls submit in shadow mode)

No strategy logic, no risk logic here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from config import CONFIG, Config
from models import (
    AccountSnapshot,
    AssetClass,
    Bar,
    ClockInfo,
    OrderResult,
    Position,
    Side,
    utcnow,
)

log = logging.getLogger("broker")

_TF_UNITS = {
    "min": TimeFrameUnit.Minute,
    "minute": TimeFrameUnit.Minute,
    "h": TimeFrameUnit.Hour,
    "hour": TimeFrameUnit.Hour,
    "d": TimeFrameUnit.Day,
    "day": TimeFrameUnit.Day,
    "w": TimeFrameUnit.Week,
    "week": TimeFrameUnit.Week,
}
_UNIT_MINUTES = {
    TimeFrameUnit.Minute: 1,
    TimeFrameUnit.Hour: 60,
    TimeFrameUnit.Day: 60 * 24,
    TimeFrameUnit.Week: 60 * 24 * 7,
}


class BrokerError(RuntimeError):
    """Any failure talking to Alpaca, normalised."""


class ShadowModeError(BrokerError):
    """Raised if something tries to submit a real order in shadow mode."""


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_timeframe(text: str) -> TimeFrame:
    """'15Min' -> TimeFrame(15, Minute); '1H' -> TimeFrame(1, Hour); '1D' -> Day."""
    raw = text.strip().lower()
    num = ""
    i = 0
    while i < len(raw) and (raw[i].isdigit()):
        num += raw[i]
        i += 1
    amount = int(num) if num else 1
    unit_key = raw[i:] or "d"
    if unit_key not in _TF_UNITS:
        raise BrokerError(
            f"unrecognised BAR_TIMEFRAME {text!r} "
            f"(use e.g. 1Min, 5Min, 15Min, 1H, 1D)"
        )
    return TimeFrame(amount, _TF_UNITS[unit_key])


class AlpacaBroker:
    def __init__(self, config: Config | None = None):
        self.config = config or CONFIG
        c = self.config

        self._paper = c.is_paper_endpoint
        self.trading = TradingClient(
            c.alpaca_api_key, c.alpaca_secret_key, paper=self._paper
        )
        self.stock_data = StockHistoricalDataClient(
            c.alpaca_api_key, c.alpaca_secret_key
        )
        self.crypto_data = CryptoHistoricalDataClient(
            c.alpaca_api_key, c.alpaca_secret_key
        )

        # "BTCUSD" -> "BTC/USD" for the configured universe
        self._slash = {s.replace("/", ""): s for s in c.crypto_symbols}
        self._crypto_set = set(c.crypto_symbols) | set(self._slash)

        log.info(
            "AlpacaBroker ready: endpoint=%s (paper=%s) execution=%s",
            c.alpaca_env,
            self._paper,
            c.execution_mode,
        )

    # ------------------------------------------------------------------ #
    # symbol helpers
    # ------------------------------------------------------------------ #
    def is_crypto(self, symbol: str) -> bool:
        return (
            "/" in symbol
            or symbol in self._crypto_set
            or symbol.upper() in self._crypto_set
        )

    def normalize_symbol(self, symbol: str, asset_class: str | None = None) -> str:
        if "/" in symbol:
            return symbol
        if symbol in self._slash:
            return self._slash[symbol]
        is_crypto = (asset_class == AssetClass.CRYPTO.value) or self.is_crypto(symbol)
        if is_crypto:
            for quote in ("USDT", "USDC", "USD"):
                if symbol.endswith(quote) and len(symbol) > len(quote):
                    return f"{symbol[:-len(quote)]}/{quote}"
        return symbol

    def asset_class_of(self, symbol: str) -> AssetClass:
        return AssetClass.CRYPTO if self.is_crypto(symbol) else AssetClass.EQUITY

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #
    def get_account(self) -> AccountSnapshot:
        try:
            a = self.trading.get_account()
        except APIError as exc:
            raise BrokerError(f"get_account failed: {exc}") from exc
        return AccountSnapshot(
            equity=_f(a.equity),
            cash=_f(a.cash),
            buying_power=_f(a.buying_power),
            currency=getattr(a, "currency", "USD") or "USD",
            last_equity=_f(a.last_equity),
            raw_status=str(getattr(a, "status", "")),
        )

    # ------------------------------------------------------------------ #
    # positions
    # ------------------------------------------------------------------ #
    def get_positions(self) -> list[Position]:
        try:
            raw = self.trading.get_all_positions()
        except APIError as exc:
            raise BrokerError(f"get_all_positions failed: {exc}") from exc
        return [self._position(p) for p in raw]

    def get_position(self, symbol: str) -> Position | None:
        target = self.normalize_symbol(symbol)
        for p in self.get_positions():
            if p.symbol == target:
                return p
        return None

    def _position(self, p) -> Position:
        ac_raw = getattr(p.asset_class, "value", str(p.asset_class))
        asset_class = (
            AssetClass.CRYPTO if "crypto" in ac_raw else AssetClass.EQUITY
        )
        symbol = self.normalize_symbol(p.symbol, ac_raw)
        qty = _f(p.qty)
        if str(getattr(p, "side", "long")).endswith("short"):
            qty = -abs(qty)
        return Position(
            symbol=symbol,
            asset_class=asset_class,
            qty=qty,
            avg_entry_price=_f(p.avg_entry_price),
            current_price=_f(p.current_price),
            market_value=_f(p.market_value),
            cost_basis=_f(p.cost_basis),
            unrealized_pl=_f(p.unrealized_pl),
            unrealized_pl_pct=_f(getattr(p, "unrealized_plpc", 0.0)) * 100.0,
        )

    # ------------------------------------------------------------------ #
    # market data
    # ------------------------------------------------------------------ #
    def get_price(self, symbol: str) -> float:
        return self.get_prices([symbol]).get(self.normalize_symbol(symbol), 0.0)

    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        equities = [s for s in symbols if not self.is_crypto(s)]
        cryptos = [self.normalize_symbol(s) for s in symbols if self.is_crypto(s)]
        out: dict[str, float] = {}

        if equities:
            try:
                res = self.stock_data.get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=equities)
                )
                for sym, trade in res.items():
                    out[sym] = _f(trade.price)
            except APIError as exc:
                raise BrokerError(f"stock latest trade failed: {exc}") from exc

        if cryptos:
            try:
                res = self.crypto_data.get_crypto_latest_trade(
                    CryptoLatestTradeRequest(symbol_or_symbols=cryptos)
                )
                for sym, trade in res.items():
                    out[self.normalize_symbol(sym)] = _f(trade.price)
            except APIError as exc:
                raise BrokerError(f"crypto latest trade failed: {exc}") from exc

        return out

    def get_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        lookback: int | None = None,
    ) -> list[Bar]:
        tf_text = timeframe or self.config.bar_timeframe
        limit = lookback or self.config.bar_lookback
        tf = parse_timeframe(tf_text)
        unit_minutes = _UNIT_MINUTES.get(tf.unit_value, 60) * tf.amount_value
        crypto = self.is_crypto(symbol)
        intraday = tf.unit_value in (TimeFrameUnit.Minute, TimeFrameUnit.Hour)

        # Alpaca returns bars going FORWARD from `start`, so we ask for a wide
        # window (no server-side limit) and slice the most recent `limit` locally.
        # Equities only trade ~4.6h of each calendar day, so pad hard for those.
        if crypto:
            pad = 1.3
        elif intraday:
            pad = 8.0
        else:
            pad = 2.5
        start = utcnow() - timedelta(minutes=unit_minutes * limit * pad)

        try:
            if crypto:
                sym = self.normalize_symbol(symbol)
                res = self.crypto_data.get_crypto_bars(
                    CryptoBarsRequest(
                        symbol_or_symbols=[sym], timeframe=tf, start=start
                    )
                )
            else:
                sym = symbol
                res = self.stock_data.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=[sym], timeframe=tf, start=start
                    )
                )
        except APIError as exc:
            raise BrokerError(f"get_bars({symbol}) failed: {exc}") from exc

        data = res.data.get(sym) or res.data.get(self.normalize_symbol(symbol)) or []
        bars = [
            Bar(
                timestamp=b.timestamp,
                open=_f(b.open),
                high=_f(b.high),
                low=_f(b.low),
                close=_f(b.close),
                volume=_f(b.volume),
            )
            for b in data
        ]
        bars.sort(key=lambda x: x.timestamp)
        return bars[-limit:]

    # ------------------------------------------------------------------ #
    # clock / market status
    # ------------------------------------------------------------------ #
    def get_clock(self) -> ClockInfo:
        try:
            c = self.trading.get_clock()
        except APIError as exc:
            raise BrokerError(f"get_clock failed: {exc}") from exc
        return ClockInfo(
            timestamp=c.timestamp,
            is_open=bool(c.is_open),
            next_open=getattr(c, "next_open", None),
            next_close=getattr(c, "next_close", None),
        )

    def is_equity_market_open(self) -> bool:
        return self.get_clock().is_open

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def get_orders(self, limit: int = 50) -> list[OrderResult]:
        try:
            raw = self.trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
            )
        except APIError as exc:
            raise BrokerError(f"get_orders failed: {exc}") from exc
        return [self._order(o) for o in raw]

    def submit_market_order(
        self,
        symbol: str,
        side: Side,
        *,
        qty: float | None = None,
        notional: float | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        """Submit a market order. Raises ShadowModeError in shadow mode."""
        if self.config.is_shadow:
            raise ShadowModeError(
                "EXECUTION_MODE=shadow -- submit_market_order must not be called"
            )
        if (qty is None) == (notional is None):
            raise BrokerError("provide exactly one of qty= or notional=")

        crypto = self.is_crypto(symbol)
        sym = self.normalize_symbol(symbol) if crypto else symbol
        tif = TimeInForce.GTC if crypto else TimeInForce.DAY
        order_side = OrderSide.BUY if side == Side.BUY else OrderSide.SELL

        req = MarketOrderRequest(
            symbol=sym,
            qty=qty,
            notional=round(notional, 2) if notional is not None else None,
            side=order_side,
            time_in_force=tif,
            client_order_id=client_order_id,
        )
        log.warning(
            "SUBMIT %s %s %s (endpoint=%s)",
            order_side.value,
            sym,
            f"qty={qty}" if qty is not None else f"notional=${notional}",
            self.config.alpaca_env,
        )
        try:
            o = self.trading.submit_order(req)
        except APIError as exc:
            raise BrokerError(f"submit_order({sym}) failed: {exc}") from exc
        return self._order(o)

    def close_position(
        self, symbol: str, *, percentage: float | None = None
    ) -> OrderResult:
        if self.config.is_shadow:
            raise ShadowModeError("EXECUTION_MODE=shadow -- close_position blocked")
        sym = self.normalize_symbol(symbol)
        try:
            from alpaca.trading.requests import ClosePositionRequest

            opts = (
                ClosePositionRequest(percentage=str(percentage))
                if percentage is not None
                else None
            )
            o = self.trading.close_position(sym, close_options=opts)
        except APIError as exc:
            raise BrokerError(f"close_position({sym}) failed: {exc}") from exc
        return self._order(o)

    def _order(self, o) -> OrderResult:
        side_raw = getattr(o.side, "value", str(o.side))
        return OrderResult(
            id=str(o.id),
            client_order_id=str(getattr(o, "client_order_id", "") or ""),
            symbol=self.normalize_symbol(o.symbol),
            side=Side.BUY if side_raw == "buy" else Side.SELL,
            qty=_f(o.qty) if getattr(o, "qty", None) is not None else None,
            notional=_f(o.notional) if getattr(o, "notional", None) is not None else None,
            filled_qty=_f(getattr(o, "filled_qty", 0.0)),
            filled_avg_price=(
                _f(o.filled_avg_price)
                if getattr(o, "filled_avg_price", None) is not None
                else None
            ),
            status=str(getattr(o.status, "value", o.status)),
            type=str(getattr(o, "order_type", getattr(o, "type", "market"))),
            submitted_at=getattr(o, "submitted_at", None),
            filled_at=getattr(o, "filled_at", None),
        )


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level="INFO")
    b = AlpacaBroker()
    acct = b.get_account()
    print("account:", acct)
    print("positions:", b.get_positions())
