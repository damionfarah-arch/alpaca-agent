"""Hard safety rails. Every order the agent wants to place is run through
`RiskManager.evaluate_order()` first; the agent also calls `kill_switch_active()`
and `daily_loss_halt()` at the top of every loop iteration.

Nothing here places or cancels orders -- it only says yes/no and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import Config
from models import AccountSnapshot, Position, Side


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    checks: list[RiskCheck] = field(default_factory=list)

    def summary(self) -> str:
        marks = ", ".join(
            f"{c.name}:{'ok' if c.passed else 'FAIL'}" for c in self.checks
        )
        return f"{'ALLOW' if self.allowed else 'BLOCK'} ({marks})"


@dataclass(frozen=True)
class HaltDecision:
    halted: bool
    reason: str


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    notional_usd: float          # estimated USD value of the order
    qty: float | None = None
    reduce_only: bool = False    # True when this sell only trims/closes a position


# --------------------------------------------------------------------------- #
# loop-level halts
# --------------------------------------------------------------------------- #
def kill_switch_active(config: Config) -> HaltDecision:
    """Checked at the top of every loop iteration."""
    if config.kill_switch_env:
        return HaltDecision(True, "kill switch: KILL_SWITCH env var is true")
    if config.kill_switch_file.exists():
        return HaltDecision(
            True, f"kill switch: file present at {config.kill_switch_file}"
        )
    return HaltDecision(False, "")


def daily_loss_halt(
    config: Config,
    *,
    account: AccountSnapshot,
    opening_equity: float | None,
) -> HaltDecision:
    """Pause new trading if the day's losses breach either configured limit.

    `opening_equity` -- account equity at the first loop of the current UTC day.
    Falls back to Alpaca's `last_equity` (prior close). Equity already reflects
    both realized and unrealized P&L, so day P&L is simply equity - opening.
    """
    base_equity = opening_equity if opening_equity is not None else account.last_equity
    day_pl = account.equity - base_equity

    limit_usd = config.risk.max_daily_loss_usd
    if limit_usd > 0 and day_pl <= -limit_usd:
        return HaltDecision(
            True,
            f"daily loss limit hit: day P&L ${day_pl:,.2f} <= -${limit_usd:,.2f} "
            f"(equity ${account.equity:,.2f} vs day-open ${base_equity:,.2f})",
        )

    limit_pct = config.risk.max_daily_loss_pct
    if limit_pct > 0 and base_equity > 0:
        drawdown_pct = (day_pl / base_equity) * 100.0
        if drawdown_pct <= -limit_pct:
            return HaltDecision(
                True,
                f"daily loss limit hit: down {drawdown_pct:.2f}% of opening "
                f"equity ${base_equity:,.2f} (limit -{limit_pct:.2f}%)",
            )

    return HaltDecision(False, "")


# --------------------------------------------------------------------------- #
# per-order evaluation
# --------------------------------------------------------------------------- #
class RiskManager:
    def __init__(self, config: Config):
        self.config = config
        self.r = config.risk

    def evaluate_order(
        self,
        intent: OrderIntent,
        account: AccountSnapshot,
        positions: list[Position],
    ) -> RiskDecision:
        checks: list[RiskCheck] = []

        # 0. kill switch always wins
        ks = kill_switch_active(self.config)
        checks.append(RiskCheck("kill_switch", not ks.halted, ks.reason or "inactive"))
        if ks.halted:
            return RiskDecision(False, ks.reason, checks)

        notional = abs(intent.notional_usd)

        # Sells that only reduce/close exposure lower risk -> skip size caps.
        is_reducing_sell = intent.side == Side.SELL and intent.reduce_only
        if is_reducing_sell:
            checks.append(
                RiskCheck("reduce_only_sell", True, "risk-reducing sell, size caps skipped")
            )
            return RiskDecision(True, "risk-reducing sell within rails", checks)

        # 1. per-trade notional cap
        cap = self.r.max_position_notional_usd
        ok = notional <= cap + 1e-9
        checks.append(
            RiskCheck(
                "max_position_notional",
                ok,
                f"order ${notional:,.2f} vs cap ${cap:,.2f}",
            )
        )
        if not ok:
            return RiskDecision(
                False,
                f"order ${notional:,.2f} exceeds per-trade cap ${cap:,.2f}",
                checks,
            )

        # 2. total allocation cap (current gross exposure + this order)
        gross_exposure = sum(abs(p.market_value) for p in positions)
        projected = gross_exposure + (notional if intent.side == Side.BUY else 0.0)
        total_cap = self.r.max_total_allocation_usd
        ok = projected <= total_cap + 1e-9
        checks.append(
            RiskCheck(
                "max_total_allocation",
                ok,
                f"exposure ${gross_exposure:,.2f} + order -> ${projected:,.2f} "
                f"vs cap ${total_cap:,.2f}",
            )
        )
        if not ok:
            return RiskDecision(
                False,
                f"projected allocation ${projected:,.2f} exceeds total cap "
                f"${total_cap:,.2f}",
                checks,
            )

        # 3. buying power sanity (buys only)
        if intent.side == Side.BUY:
            ok = notional <= account.buying_power + 1e-9
            checks.append(
                RiskCheck(
                    "buying_power",
                    ok,
                    f"order ${notional:,.2f} vs buying power ${account.buying_power:,.2f}",
                )
            )
            if not ok:
                return RiskDecision(
                    False,
                    f"insufficient buying power: need ${notional:,.2f}, "
                    f"have ${account.buying_power:,.2f}",
                    checks,
                )

        return RiskDecision(True, "within all risk rails", checks)
