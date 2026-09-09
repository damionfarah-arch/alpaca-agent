"""Rule-based strategies. Every evaluation returns a `StrategySignal` with a
plain-English `reason` -- no black boxes.

v1 ships one strategy: moving-average crossover, in its robust *state-based*
form -- the strategy reports a TARGET regime every bar (fast MA above slow =>
bullish => target long; fast below slow => bearish => target flat). The agent
only trades when the target differs from what is currently held, so a missed
poll never means a missed entry/exit. When a cross happened on the most recent
bar the reason string says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import Config
from models import Bar

BUY = "buy"      # target regime: long
SELL = "sell"    # target regime: flat
HOLD = "hold"    # not enough data / neutral


@dataclass(frozen=True)
class StrategySignal:
    action: str                       # "buy" | "sell" | "hold"
    reason: str                       # human-readable, always populated
    price: float | None               # last close used for the decision
    fast_ma: float | None = None
    slow_ma: float | None = None
    indicators: dict[str, Any] = field(default_factory=dict)


def sma(values: list[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / len(window)


class MACrossover:
    name = "ma_crossover"

    def __init__(self, fast: int, slow: int, min_spread_pct: float = 0.0):
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        self.fast = fast
        self.slow = slow
        self.min_spread_pct = max(0.0, min_spread_pct)

    def describe(self) -> str:
        band = (
            f", deadband={self.min_spread_pct:g}%" if self.min_spread_pct else ""
        )
        return f"MA crossover (fast={self.fast}, slow={self.slow}, state-based{band})"

    def evaluate(self, bars: list[Bar]) -> StrategySignal:
        closes = [b.close for b in bars]
        need = self.slow + 1
        if len(closes) < need:
            return StrategySignal(
                action=HOLD,
                reason=(
                    f"insufficient data: have {len(closes)} bars, need {need} "
                    f"(slow MA {self.slow} + 1)."
                ),
                price=closes[-1] if closes else None,
                indicators={"bars": len(closes), "bars_needed": need},
            )

        prev_fast = sma(closes[:-1], self.fast)
        prev_slow = sma(closes[:-1], self.slow)
        cur_fast = sma(closes, self.fast)
        cur_slow = sma(closes, self.slow)
        price = closes[-1]

        spread = cur_fast - cur_slow
        spread_pct = (spread / cur_slow * 100.0) if cur_slow else 0.0
        prev_spread = prev_fast - prev_slow

        crossed_up = prev_spread <= 0 < spread
        crossed_down = prev_spread >= 0 > spread

        ind: dict[str, Any] = {
            "fast": round(cur_fast, 6),
            "slow": round(cur_slow, 6),
            "prev_fast": round(prev_fast, 6),
            "prev_slow": round(prev_slow, 6),
            "spread": round(spread, 6),
            "spread_pct": round(spread_pct, 4),
            "last_price": round(price, 6),
            "crossed_up": crossed_up,
            "crossed_down": crossed_down,
        }

        if crossed_up:
            cross_note = (
                f" GOLDEN CROSS on the latest bar (prev fast {prev_fast:,.2f} "
                f"<= prev slow {prev_slow:,.2f})."
            )
        elif crossed_down:
            cross_note = (
                f" DEATH CROSS on the latest bar (prev fast {prev_fast:,.2f} "
                f">= prev slow {prev_slow:,.2f})."
            )
        else:
            cross_note = ""

        if abs(spread_pct) < self.min_spread_pct:
            return StrategySignal(
                action=HOLD,
                reason=(
                    f"NEUTRAL ZONE: fast MA({self.fast}) {cur_fast:,.2f} vs slow "
                    f"MA({self.slow}) {cur_slow:,.2f}, spread {spread_pct:+.2f}% is "
                    f"within the +/-{self.min_spread_pct:g}% deadband -- too weak to "
                    f"act on, holding current position. Last price {price:,.2f}."
                    f"{cross_note}"
                ),
                price=price, fast_ma=cur_fast, slow_ma=cur_slow, indicators=ind,
            )

        if spread > 0:
            return StrategySignal(
                action=BUY,
                reason=(
                    f"BULLISH regime -> target LONG. fast MA({self.fast}) "
                    f"{cur_fast:,.2f} > slow MA({self.slow}) {cur_slow:,.2f} "
                    f"(spread +{spread_pct:.2f}%). Last price {price:,.2f}.{cross_note}"
                ),
                price=price, fast_ma=cur_fast, slow_ma=cur_slow, indicators=ind,
            )
        if spread < 0:
            return StrategySignal(
                action=SELL,
                reason=(
                    f"BEARISH regime -> target FLAT. fast MA({self.fast}) "
                    f"{cur_fast:,.2f} < slow MA({self.slow}) {cur_slow:,.2f} "
                    f"(spread {spread_pct:.2f}%). Last price {price:,.2f}.{cross_note}"
                ),
                price=price, fast_ma=cur_fast, slow_ma=cur_slow, indicators=ind,
            )
        return StrategySignal(
            action=HOLD,
            reason=(
                f"NEUTRAL: fast MA({self.fast}) == slow MA({self.slow}) "
                f"at {cur_fast:,.2f}. No regime. Last price {price:,.2f}."
            ),
            price=price, fast_ma=cur_fast, slow_ma=cur_slow, indicators=ind,
        )


def get_strategy(config: Config):
    if config.strategy == "ma_crossover":
        return MACrossover(config.ma_fast, config.ma_slow, config.ma_min_spread_pct)
    raise ValueError(f"unknown STRATEGY {config.strategy!r} (supported: ma_crossover)")
