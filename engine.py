"""Shared trade-planning logic used by BOTH the live agent and the backtester,
so a backtest tests exactly what runs in production.

Pure functions only -- no DB, no broker, no clock. Long-only, one lot per
symbol, full-position exits.
"""

from __future__ import annotations

from dataclasses import dataclass

from models import Side
from risk import OrderIntent
from strategy import BUY, HOLD, SELL


@dataclass(frozen=True)
class PlannedTrade:
    intent_action: str                 # "buy" | "sell" | "hold"
    order_intent: OrderIntent | None    # None when intent_action == "hold"
    position_reason: str | None         # why intent differs from the raw signal


def plan_trade(
    signal_action: str,
    *,
    held_qty: float,
    symbol: str,
    notional_cap: float,
    ref_price: float | None,
) -> PlannedTrade:
    """Translate a raw strategy signal into an intended order, given what we hold.

    - BUY  + flat      -> buy `notional_cap` USD
    - BUY  + long      -> hold (already long; never add)
    - SELL + long      -> sell the whole position (reduce-only)
    - SELL + flat      -> hold (nothing to sell)
    - HOLD             -> hold
    """
    if signal_action == BUY:
        if held_qty > 0:
            return PlannedTrade(
                HOLD, None, f"already long {held_qty:g} {symbol}; not adding."
            )
        return PlannedTrade(
            BUY,
            OrderIntent(symbol=symbol, side=Side.BUY, notional_usd=notional_cap),
            None,
        )

    if signal_action == SELL:
        if held_qty > 0:
            px = ref_price or 0.0
            return PlannedTrade(
                SELL,
                OrderIntent(
                    symbol=symbol,
                    side=Side.SELL,
                    notional_usd=abs(held_qty) * px,
                    qty=abs(held_qty),
                    reduce_only=True,
                ),
                None,
            )
        return PlannedTrade(HOLD, None, "no open position to sell.")

    return PlannedTrade(HOLD, None, None)


def check_stop_take(
    unrealized_pl_pct: float | None,
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> tuple[bool, str | None]:
    """Force-exit check for an open long position, independent of the strategy
    signal. Either threshold set to 0 disables that side.

    Returns (should_exit, reason). Stop-loss is checked before take-profit.
    """
    if unrealized_pl_pct is None:
        return False, None
    if stop_loss_pct > 0 and unrealized_pl_pct <= -stop_loss_pct:
        return True, (
            f"STOP-LOSS triggered: position down {unrealized_pl_pct:.2f}% "
            f"(limit -{stop_loss_pct:g}%)."
        )
    if take_profit_pct > 0 and unrealized_pl_pct >= take_profit_pct:
        return True, (
            f"TAKE-PROFIT triggered: position up {unrealized_pl_pct:.2f}% "
            f"(target +{take_profit_pct:g}%)."
        )
    return False, None
