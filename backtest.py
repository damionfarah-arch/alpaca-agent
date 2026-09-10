"""Replay the live strategy over historical data -- the "what if I'd run this
last year?" machine.

    .venv/bin/python -m backtest --start 2024-01-01
    .venv/bin/python -m backtest --start 2024-01-01 --symbols BTC/USD,ETH/USD \
        --timeframe 1H --ma-fast 10 --ma-slow 30

Uses the SAME strategy.py and engine.py the live agent uses, so results reflect
what actually runs. Historical bars are cached under data/bars_cache/.

Backtests flatter reality: fills are at the signal bar's close with no slippage,
and past performance is not a forecast. Treat it as a filter for bad ideas.
"""

from __future__ import annotations

import argparse
import math
import pickle
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import CONFIG
from engine import plan_trade
from models import Bar, Side
from strategy import MACrossover

CACHE = Path("data/bars_cache")
_SPARK = "▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------- #
@dataclass
class BTPosition:
    symbol: str
    qty: float
    entry_price: float
    cost_basis: float          # cash paid, including entry fee
    opened_at: datetime


@dataclass
class BTTrade:
    ts: datetime
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    realized_pl: float | None
    reason: str


@dataclass
class BacktestResult:
    params: dict
    start: datetime
    end: datetime
    starting_cash: float
    final_equity: float
    curve: list[tuple[datetime, float]]
    trades: list[BTTrade]
    buy_hold_equity: float
    in_market_frac: float

    # ---- derived metrics ----
    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.starting_cash - 1) * 100

    @property
    def buy_hold_return_pct(self) -> float:
        return (self.buy_hold_equity / self.starting_cash - 1) * 100

    @property
    def years(self) -> float:
        return max((self.end - self.start).total_seconds() / (365.25 * 86400), 1e-9)

    @property
    def cagr_pct(self) -> float:
        r = self.final_equity / self.starting_cash
        return (r ** (1 / self.years) - 1) * 100 if r > 0 else -100.0

    @property
    def max_drawdown_pct(self) -> float:
        peak = -math.inf
        worst = 0.0
        for _, eq in self.curve:
            peak = max(peak, eq)
            if peak > 0:
                worst = min(worst, (eq - peak) / peak)
        return worst * 100

    @property
    def round_trips(self) -> list[float]:
        return [t.realized_pl for t in self.trades if t.side == "sell" and t.realized_pl is not None]

    @property
    def wins(self) -> list[float]:
        return [p for p in self.round_trips if p > 0]

    @property
    def losses(self) -> list[float]:
        return [p for p in self.round_trips if p <= 0]

    @property
    def win_rate_pct(self) -> float:
        rt = self.round_trips
        return (len(self.wins) / len(rt) * 100) if rt else 0.0

    @property
    def profit_factor(self) -> float:
        gain = sum(self.wins)
        loss = abs(sum(self.losses))
        return (gain / loss) if loss > 0 else math.inf

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def ann_vol_pct(self) -> float:
        rets = self._step_returns()
        if len(rets) < 3:
            return 0.0
        return statistics.pstdev(rets) * math.sqrt(self._periods_per_year()) * 100

    @property
    def sharpe(self) -> float:
        rets = self._step_returns()
        if len(rets) < 3:
            return 0.0
        sd = statistics.pstdev(rets)
        if sd == 0:
            return 0.0
        return statistics.fmean(rets) / sd * math.sqrt(self._periods_per_year())

    def _step_returns(self) -> list[float]:
        out = []
        for (_, a), (_, b) in zip(self.curve, self.curve[1:]):
            if a > 0:
                out.append(b / a - 1)
        return out

    def _periods_per_year(self) -> float:
        if len(self.curve) < 3:
            return 252.0
        deltas = [
            (self.curve[i + 1][0] - self.curve[i][0]).total_seconds()
            for i in range(min(len(self.curve) - 1, 500))
        ]
        med = statistics.median([d for d in deltas if d > 0] or [86400])
        return (365.25 * 86400) / med


# --------------------------------------------------------------------------- #
def load_bars(broker, symbol: str, timeframe: str, start: datetime, end: datetime,
              *, use_cache: bool = True) -> list[Bar]:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = f"{symbol.replace('/', '-')}_{timeframe}_{start:%Y%m%d}_{end:%Y%m%d}.pkl"
    p = CACHE / key
    if use_cache and p.exists():
        try:
            return pickle.loads(p.read_bytes())
        except Exception:
            pass
    bars = broker.get_bars_range(symbol, timeframe, start, end)
    try:
        p.write_bytes(pickle.dumps(bars))
    except Exception:
        pass
    return bars


def run_backtest(
    symbols: list[str],
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    ma_fast: int,
    ma_slow: int,
    deadband: float,
    starting_cash: float,
    per_trade_usd: float,
    max_alloc_usd: float,
    crypto_fee_bps: float,
    equity_fee_bps: float,
    use_cache: bool = True,
    verbose: bool = True,
) -> BacktestResult:
    from broker_alpaca import AlpacaBroker

    broker = AlpacaBroker()
    strat = MACrossover(ma_fast, ma_slow, deadband)
    warmup = ma_slow + 3
    win = ma_slow + 5  # trailing bars handed to the strategy each step

    series: dict[str, list[Bar]] = {}
    for s in symbols:
        bars = load_bars(broker, s, timeframe, start, end, use_cache=use_cache)
        series[s] = bars
        if verbose:
            span = f"{bars[0].timestamp:%Y-%m-%d} → {bars[-1].timestamp:%Y-%m-%d}" if bars else "no data"
            print(f"  {s:<10} {len(bars):>6} bars   {span}", file=sys.stderr)

    if not any(series.values()):
        raise SystemExit("no historical bars returned for any symbol")

    bar_at = {s: {b.timestamp: b for b in series[s]} for s in symbols}
    ptr = {s: 0 for s in symbols}
    all_ts = sorted({b.timestamp for bars in series.values() for b in bars})

    cash = starting_cash
    positions: dict[str, BTPosition] = {}
    trades: list[BTTrade] = []
    curve: list[tuple[datetime, float]] = []
    last_px: dict[str, float] = {}
    in_market_steps = 0

    def fee_bps(sym: str) -> float:
        return crypto_fee_bps if broker.is_crypto(sym) else equity_fee_bps

    for ts in all_ts:
        for s in symbols:
            b = bar_at[s].get(ts)
            if b is None:
                continue
            ptr[s] += 1
            last_px[s] = b.close
            if ptr[s] < warmup:
                continue

            hist = series[s][max(0, ptr[s] - win): ptr[s]]
            sig = strat.evaluate(hist)
            held = positions[s].qty if s in positions else 0.0
            planned = plan_trade(
                sig.action, held_qty=held, symbol=s,
                notional_cap=per_trade_usd, ref_price=b.close,
            )
            oi = planned.order_intent
            if oi is None:
                continue

            if oi.side == Side.BUY:
                gross = sum(p.qty * last_px.get(k, p.entry_price) for k, p in positions.items())
                if gross + per_trade_usd > max_alloc_usd + 1e-9:
                    continue  # total-allocation cap
                fee = per_trade_usd * fee_bps(s) / 1e4
                if per_trade_usd + fee > cash + 1e-9:
                    continue  # insufficient cash
                qty = per_trade_usd / b.close
                cash -= per_trade_usd + fee
                positions[s] = BTPosition(s, qty, b.close, per_trade_usd + fee, ts)
                trades.append(BTTrade(ts, s, "buy", qty, b.close, fee, None, sig.reason))
            else:  # SELL -> close the whole position
                p = positions.pop(s)
                proceeds = p.qty * b.close
                fee = proceeds * fee_bps(s) / 1e4
                pnl = (proceeds - fee) - p.cost_basis
                cash += proceeds - fee
                trades.append(BTTrade(ts, s, "sell", p.qty, b.close, fee, pnl, sig.reason))

        mv = sum(p.qty * last_px.get(k, p.entry_price) for k, p in positions.items())
        curve.append((ts, cash + mv))
        if positions:
            in_market_steps += 1

    # buy & hold: equal-weight across symbols that have data
    bh = 0.0
    funded = [s for s in symbols if series[s]]
    per = starting_cash / len(funded) if funded else 0.0
    for s in funded:
        first, last = series[s][0].close, series[s][-1].close
        bh += per * (last / first) if first else per

    return BacktestResult(
        params={
            "symbols": symbols, "timeframe": timeframe,
            "ma_fast": ma_fast, "ma_slow": ma_slow, "deadband": deadband,
            "per_trade_usd": per_trade_usd, "max_alloc_usd": max_alloc_usd,
            "crypto_fee_bps": crypto_fee_bps, "equity_fee_bps": equity_fee_bps,
        },
        start=start, end=end, starting_cash=starting_cash,
        final_equity=curve[-1][1] if curve else starting_cash,
        curve=curve, trades=trades, buy_hold_equity=bh,
        in_market_frac=(in_market_steps / len(curve)) if curve else 0.0,
    )


# --------------------------------------------------------------------------- #
def sparkline(curve: list[tuple[datetime, float]], width: int = 64) -> str:
    if len(curve) < 2:
        return ""
    vals = [v for _, v in curve]
    step = max(1, len(vals) // width)
    sampled = vals[::step]
    lo, hi = min(sampled), max(sampled)
    rng = hi - lo or 1
    return "".join(_SPARK[min(7, int((v - lo) / rng * 7))] for v in sampled)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _pct(x: float) -> str:
    return f"{x:+.2f}%"


def print_report(r: BacktestResult) -> None:
    p = r.params
    verdict = "BEAT" if r.total_return_pct > r.buy_hold_return_pct else "LOST TO"
    print()
    print("═" * 68)
    print(f" BACKTEST  {','.join(p['symbols'])}")
    print(f" {r.start:%Y-%m-%d} → {r.end:%Y-%m-%d}  ({r.years:.2f}y)  "
          f"tf={p['timeframe']}  MA {p['ma_fast']}/{p['ma_slow']}  "
          f"deadband {p['deadband']:g}%")
    print(f" ${r.starting_cash:,.0f} start  ·  ${p['per_trade_usd']:,.0f}/trade  ·  "
          f"${p['max_alloc_usd']:,.0f} max deployed  ·  "
          f"crypto fee {p['crypto_fee_bps']:g}bps")
    print("═" * 68)
    print()
    print(f"  equity  {sparkline(r.curve)}")
    print()
    print(f"  {'Final equity':<24} {_money(r.final_equity)}")
    print(f"  {'Total return':<24} {_pct(r.total_return_pct)}")
    print(f"  {'Annualised (CAGR)':<24} {_pct(r.cagr_pct)}")
    print(f"  {'Max drawdown':<24} {_pct(r.max_drawdown_pct)}   (worst peak-to-trough dip)")
    print(f"  {'Annualised volatility':<24} {r.ann_vol_pct:.1f}%")
    print(f"  {'Sharpe (rf=0)':<24} {r.sharpe:.2f}")
    print(f"  {'Time in the market':<24} {r.in_market_frac * 100:.0f}%")
    print()
    print(f"  {'Round-trip trades':<24} {len(r.round_trips)}   "
          f"({len(r.trades)} orders incl. still-open)")
    print(f"  {'Win rate':<24} {r.win_rate_pct:.0f}%   "
          f"({len(r.wins)}W / {len(r.losses)}L)")
    if r.wins:
        print(f"  {'Avg win':<24} {_money(statistics.fmean(r.wins))}")
    if r.losses:
        print(f"  {'Avg loss':<24} {_money(statistics.fmean(r.losses))}")
    pf = r.profit_factor
    print(f"  {'Profit factor':<24} {'∞' if pf == math.inf else f'{pf:.2f}'}   "
          f"(gross win / gross loss)")
    print(f"  {'Total fees paid':<24} {_money(r.total_fees)}")
    print()
    print(f"  {'Buy & hold instead':<24} {_money(r.buy_hold_equity)}  "
          f"({_pct(r.buy_hold_return_pct)})")
    print(f"  {'>>> strategy':<24} {verdict} buy & hold by "
          f"{abs(r.total_return_pct - r.buy_hold_return_pct):.2f} pts")
    print()
    if r.trades:
        print("  last 8 trades:")
        for t in r.trades[-8:]:
            tail = (f"  P&L {_money(t.realized_pl)}" if t.realized_pl is not None else "")
            print(f"    {t.ts:%Y-%m-%d %H:%M}  {t.side.upper():<4} {t.symbol:<9} "
                  f"{t.qty:.6g} @ {_money(t.price)}  fee {_money(t.fee)}{tail}")
    print()


def write_csv(r: BacktestResult, path: str) -> None:
    import csv

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "equity"])
        for ts, eq in r.curve:
            w.writerow([ts.isoformat(), f"{eq:.4f}"])
    print(f"  wrote equity curve -> {path}")


# --------------------------------------------------------------------------- #
def _date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the live MA-crossover strategy")
    ap.add_argument("--start", required=True, type=_date, help="YYYY-MM-DD")
    ap.add_argument("--end", type=_date, default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--symbols", default=",".join(CONFIG.all_symbols),
                    help="comma-separated (default: your configured universe)")
    ap.add_argument("--timeframe", default=CONFIG.bar_timeframe)
    ap.add_argument("--ma-fast", type=int, default=CONFIG.ma_fast)
    ap.add_argument("--ma-slow", type=int, default=CONFIG.ma_slow)
    ap.add_argument("--deadband", type=float, default=CONFIG.ma_min_spread_pct)
    ap.add_argument("--cash", type=float, default=CONFIG.shadow_starting_cash)
    ap.add_argument("--per-trade", type=float, default=CONFIG.risk.max_position_notional_usd)
    ap.add_argument("--max-alloc", type=float, default=CONFIG.risk.max_total_allocation_usd)
    ap.add_argument("--crypto-fee-bps", type=float, default=25.0,
                    help="crypto trading fee in basis points (default 25 = 0.25%%)")
    ap.add_argument("--equity-fee-bps", type=float, default=0.0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--csv", default=None, help="write the equity curve to this path")
    args = ap.parse_args(argv)

    end = args.end or datetime.now(timezone.utc)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.ma_fast >= args.ma_slow:
        raise SystemExit("--ma-fast must be < --ma-slow")

    print("fetching bars...", file=sys.stderr)
    r = run_backtest(
        symbols, args.timeframe, args.start, end,
        ma_fast=args.ma_fast, ma_slow=args.ma_slow, deadband=args.deadband,
        starting_cash=args.cash, per_trade_usd=args.per_trade,
        max_alloc_usd=args.max_alloc,
        crypto_fee_bps=args.crypto_fee_bps, equity_fee_bps=args.equity_fee_bps,
        use_cache=not args.no_cache,
    )
    print_report(r)
    if args.csv:
        write_csv(r, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
