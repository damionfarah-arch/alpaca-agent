"""Central configuration, loaded once from environment / .env.

Nothing else in the project reads os.environ directly -- import `CONFIG` from
here. All money is USD.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # no-op if .env is absent (e.g. on the VPS using real env vars)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _str(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and (val is None or val == ""):
        raise ConfigError(f"Required env var {key} is not set")
    return val if val is not None else ""


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise ConfigError(f"env var {key}={raw!r} is not an integer") from exc


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise ConfigError(f"env var {key}={raw!r} is not a number") from exc


def _csv(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


class ConfigError(RuntimeError):
    """Raised on invalid / missing configuration."""


# --------------------------------------------------------------------------- #
# config object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RiskConfig:
    max_position_notional_usd: float
    max_total_allocation_usd: float
    max_daily_loss_usd: float
    max_daily_loss_pct: float


@dataclass(frozen=True)
class Config:
    # --- Alpaca ---
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_env: str            # "paper" | "live"
    allow_live: bool

    # --- execution ---
    execution_mode: str        # "shadow" | "live"
    shadow_starting_cash: float # simulated USD cash for the shadow portfolio

    # --- universe ---
    equity_symbols: list[str]
    crypto_symbols: list[str]

    # --- strategy ---
    strategy: str
    ma_fast: int
    ma_slow: int
    ma_min_spread_pct: float   # deadband: |fast-slow|/slow below this = neutral
    bar_timeframe: str
    bar_lookback: int

    # --- loop ---
    loop_interval_seconds: int
    trade_equity_when_closed: bool

    # --- risk ---
    risk: RiskConfig

    # --- kill switch ---
    kill_switch_env: bool
    kill_switch_file: Path

    # --- storage ---
    db_path: Path

    # --- dashboard ---
    dashboard_host: str
    dashboard_port: int
    dashboard_poll_seconds: int
    dashboard_user: str
    dashboard_password: str

    # --- misc ---
    log_level: str
    base_currency: str = "USD"

    # ---- derived ----
    @property
    def is_live_endpoint(self) -> bool:
        return self.alpaca_env == "live"

    @property
    def is_paper_endpoint(self) -> bool:
        return self.alpaca_env == "paper"

    @property
    def is_shadow(self) -> bool:
        return self.execution_mode == "shadow"

    @property
    def will_place_real_orders(self) -> bool:
        """True only when orders are actually submitted AND to the live endpoint."""
        return self.execution_mode == "live" and self.alpaca_env == "live"

    @property
    def all_symbols(self) -> list[str]:
        return [*self.equity_symbols, *self.crypto_symbols]

    def describe(self) -> str:
        shadow = (
            f" shadow_cash=${self.shadow_starting_cash:g}" if self.is_shadow else ""
        )
        return (
            f"endpoint={self.alpaca_env} execution={self.execution_mode}{shadow} "
            f"equities={self.equity_symbols or '-'} crypto={self.crypto_symbols or '-'} "
            f"strategy={self.strategy}(fast={self.ma_fast},slow={self.ma_slow},"
            f"tf={self.bar_timeframe}) "
            f"caps: trade<=${self.risk.max_position_notional_usd:g} "
            f"total<=${self.risk.max_total_allocation_usd:g} "
            f"dayloss<=${self.risk.max_daily_loss_usd:g}"
        )


def _load() -> Config:
    alpaca_env = _str("ALPACA_ENV", "paper").lower()
    if alpaca_env not in {"paper", "live"}:
        raise ConfigError(f"ALPACA_ENV must be 'paper' or 'live', got {alpaca_env!r}")

    execution_mode = _str("EXECUTION_MODE", "shadow").lower()
    if execution_mode not in {"shadow", "live"}:
        raise ConfigError(
            f"EXECUTION_MODE must be 'shadow' or 'live', got {execution_mode!r}"
        )

    allow_live = _bool("ALPACA_ALLOW_LIVE", False)

    # Hard gate: never touch the live endpoint without the explicit second flag.
    if alpaca_env == "live" and not allow_live:
        raise ConfigError(
            "ALPACA_ENV=live but ALPACA_ALLOW_LIVE is not 'true'. "
            "Set ALPACA_ALLOW_LIVE=true to authorise real-money endpoint access."
        )

    ma_fast = _int("MA_FAST", 20)
    ma_slow = _int("MA_SLOW", 50)
    if ma_fast >= ma_slow:
        raise ConfigError(f"MA_FAST ({ma_fast}) must be < MA_SLOW ({ma_slow})")

    risk = RiskConfig(
        max_position_notional_usd=_float("MAX_POSITION_NOTIONAL_USD", 50.0),
        max_total_allocation_usd=_float("MAX_TOTAL_ALLOCATION_USD", 200.0),
        max_daily_loss_usd=_float("MAX_DAILY_LOSS_USD", 20.0),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 0.0),
    )
    for name, value in vars(risk).items():
        if value < 0:
            raise ConfigError(f"risk.{name} must be >= 0, got {value}")
    if risk.max_position_notional_usd > risk.max_total_allocation_usd:
        raise ConfigError(
            "MAX_POSITION_NOTIONAL_USD must be <= MAX_TOTAL_ALLOCATION_USD"
        )

    db_path = Path(_str("DB_PATH", "./data/agent.db")).expanduser()

    cfg = Config(
        alpaca_api_key=_str("ALPACA_API_KEY", required=True),
        alpaca_secret_key=_str("ALPACA_SECRET_KEY", required=True),
        alpaca_env=alpaca_env,
        allow_live=allow_live,
        execution_mode=execution_mode,
        shadow_starting_cash=_float("SHADOW_STARTING_CASH", 1000.0),
        equity_symbols=_csv("EQUITY_SYMBOLS", "SPY,AAPL"),
        crypto_symbols=_csv("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD"),
        strategy=_str("STRATEGY", "ma_crossover").lower(),
        ma_fast=ma_fast,
        ma_slow=ma_slow,
        ma_min_spread_pct=_float("MA_MIN_SPREAD_PCT", 0.15),
        bar_timeframe=_str("BAR_TIMEFRAME", "1H"),
        bar_lookback=_int("BAR_LOOKBACK", 200),
        loop_interval_seconds=_int("LOOP_INTERVAL_SECONDS", 300),
        trade_equity_when_closed=_bool("TRADE_EQUITY_WHEN_CLOSED", False),
        risk=risk,
        kill_switch_env=_bool("KILL_SWITCH", False),
        kill_switch_file=Path(_str("KILL_SWITCH_FILE", "./KILL")).expanduser(),
        db_path=db_path,
        dashboard_host=_str("DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=_int("DASHBOARD_PORT", 8080),
        dashboard_poll_seconds=_int("DASHBOARD_POLL_SECONDS", 30),
        dashboard_user=_str("DASHBOARD_USER", "admin"),
        dashboard_password=_str("DASHBOARD_PASSWORD", ""),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
    return cfg


CONFIG: Config = _load()
