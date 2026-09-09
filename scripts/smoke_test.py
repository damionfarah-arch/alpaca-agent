"""Read-only connectivity check for the Alpaca wrapper.

Places NO orders. Run after filling in .env:

    .venv/bin/python -m scripts.smoke_test
"""

from __future__ import annotations

import logging
import sys

logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    from config import CONFIG
    from broker_alpaca import AlpacaBroker
    from risk import RiskManager, OrderIntent, kill_switch_active
    from models import Side

    print("\n=== CONFIG ===")
    print(" ", CONFIG.describe())
    print(f"  will place real orders: {CONFIG.will_place_real_orders}")

    ks = kill_switch_active(CONFIG)
    print(f"  kill switch active: {ks.halted} {('- ' + ks.reason) if ks.halted else ''}")

    broker = AlpacaBroker()

    print("\n=== ACCOUNT ===")
    acct = broker.get_account()
    print(f"  status        {acct.raw_status}")
    print(f"  currency      {acct.currency}")
    print(f"  equity        ${acct.equity:,.2f}")
    print(f"  cash          ${acct.cash:,.2f}")
    print(f"  buying power   ${acct.buying_power:,.2f}")
    print(f"  positions val ${acct.positions_value:,.2f}")

    print("\n=== EQUITY MARKET CLOCK ===")
    clock = broker.get_clock()
    print(f"  is_open       {clock.is_open}")
    print(f"  next_open     {clock.next_open}")
    print(f"  next_close    {clock.next_close}")

    print("\n=== POSITIONS ===")
    positions = broker.get_positions()
    if not positions:
        print("  (none)")
    for p in positions:
        print(
            f"  {p.symbol:<10} {p.asset_class.value:<9} qty={p.qty:<12g} "
            f"@ ${p.current_price:,.2f}  mv=${p.market_value:,.2f}  "
            f"uPL=${p.unrealized_pl:,.2f} ({p.unrealized_pl_pct:+.2f}%)"
        )

    print("\n=== LIVE PRICES ===")
    prices = broker.get_prices(CONFIG.all_symbols)
    for sym in CONFIG.all_symbols:
        px = prices.get(broker.normalize_symbol(sym))
        print(f"  {sym:<12} {'$' + format(px, ',.2f') if px else '(no quote)'}")

    print("\n=== BARS (strategy input sanity) ===")
    for sym in CONFIG.all_symbols:
        try:
            bars = broker.get_bars(sym)
            first = bars[0].timestamp if bars else None
            last = bars[-1].timestamp if bars else None
            tail = f"{bars[-1].close:,.2f}" if bars else "-"
            print(
                f"  {sym:<12} {len(bars):>4} bars  "
                f"{first} -> {last}  last close ${tail}"
            )
        except Exception as exc:  # noqa: BLE001 - smoke test, report and continue
            print(f"  {sym:<12} ERROR: {exc}")

    print("\n=== RECENT ORDERS ===")
    orders = broker.get_orders(limit=10)
    if not orders:
        print("  (none)")
    for o in orders:
        print(
            f"  {str(o.submitted_at):<28} {o.side.value:<4} {o.symbol:<10} "
            f"{o.status:<12} filled {o.filled_qty}"
        )

    print("\n=== RISK RAIL DRY-RUN ===")
    rm = RiskManager(CONFIG)
    sample = OrderIntent(
        symbol=CONFIG.all_symbols[0] if CONFIG.all_symbols else "SPY",
        side=Side.BUY,
        notional_usd=CONFIG.risk.max_position_notional_usd,
    )
    decision = rm.evaluate_order(sample, acct, positions)
    print(f"  sample buy ${sample.notional_usd:g} of {sample.symbol}: {decision.summary()}")
    over = OrderIntent(
        symbol=sample.symbol,
        side=Side.BUY,
        notional_usd=CONFIG.risk.max_position_notional_usd * 10 + 1,
    )
    d2 = rm.evaluate_order(over, acct, positions)
    print(f"  oversized buy: allowed={d2.allowed} reason={d2.reason!r}")

    print("\nOK - wrapper is talking to Alpaca. No orders were placed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
