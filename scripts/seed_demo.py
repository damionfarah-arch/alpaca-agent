"""Write a realistic *fake* dataset to data/demo.db so the dashboard can be
reviewed without waiting for the real agent to accumulate history.

    .venv/bin/python -m scripts.seed_demo
    DB_PATH=./data/demo.db .venv/bin/python -m dashboard      # view it

This touches ONLY data/demo.db. It never contacts Alpaca and never affects
data/agent.db.
"""

from __future__ import annotations

import math
import os
import random
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ALPACA_API_KEY", "demo")
os.environ.setdefault("ALPACA_SECRET_KEY", "demo")
os.environ["DB_PATH"] = "./data/demo.db"

import db  # noqa: E402

random.seed(7)
DB = "./data/demo.db"
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except FileNotFoundError:
        pass

conn = db.init_db(DB)
START_CASH = 1000.0
now = datetime.now(timezone.utc)
start = now - timedelta(hours=40)

UNIVERSE = [
    ("BTC/USD", "crypto", 78000.0, 0.9),
    ("ETH/USD", "crypto", 2480.0, 1.4),
    ("SPY", "us_equity", 763.0, 0.5),
    ("AAPL", "us_equity", 315.0, 0.7),
]
prices = {s: p for s, _, p, _ in UNIVERSE}
vol = {s: v for s, _, _, v in UNIVERSE}
aclass = {s: a for s, a, _, _ in UNIVERSE}

positions: dict[str, dict] = {}   # symbol -> {qty, cost}
realized = 0.0
fast_ma: dict[str, float] = {s: prices[s] for s in prices}
slow_ma: dict[str, float] = {s: prices[s] for s in prices}


def ts(dt: datetime) -> str:
    return dt.isoformat()


def _ins(table: str, **cols):
    keys = ",".join(cols)
    qs = ",".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({qs})", tuple(cols.values()))


loop_id = 0
for step in range(160):
    t = start + timedelta(minutes=15 * step)
    loop_id += 1
    market_open = (t.weekday() < 5) and (13 <= t.hour < 20)  # rough RTH in UTC
    _ins("loops", started_at=ts(t), finished_at=ts(t + timedelta(seconds=2)),
         status="ok", equity_market_open=int(market_open),
         execution_mode="shadow", alpaca_env="paper")

    # random-walk prices + moving averages
    for s in prices:
        drift = math.sin(step / 12 + hash(s) % 7) * 0.0016
        prices[s] *= 1 + drift + random.uniform(-1, 1) * 0.004 * vol[s]
        fast_ma[s] += (prices[s] - fast_ma[s]) * 0.28
        slow_ma[s] += (prices[s] - slow_ma[s]) * 0.09

    for s in prices:
        ac = aclass[s]
        is_eq = ac == "us_equity"
        if is_eq and not market_open:
            _ins("decisions", ts=ts(t), loop_id=loop_id, symbol=s, asset_class=ac,
                 signal="hold", intent="hold", executed=0, shadow=1, blocked=0,
                 price=round(prices[s], 2), reason="equity market closed -- no evaluation (crypto keeps trading).")
            continue

        spread_pct = (fast_ma[s] - slow_ma[s]) / slow_ma[s] * 100
        held = s in positions
        if spread_pct > 0.15:
            sig = "buy"
        elif spread_pct < -0.15:
            sig = "sell"
        else:
            sig = "hold"

        intent, blocked, executed, pos_reason, risk_reason = "hold", 0, 0, None, None
        reason = (f"{'BULLISH' if spread_pct>0 else 'BEARISH' if spread_pct<0 else 'NEUTRAL'} "
                  f"regime. fast MA(20) {fast_ma[s]:,.2f} vs slow MA(50) {slow_ma[s]:,.2f} "
                  f"(spread {spread_pct:+.2f}%). Last price {prices[s]:,.2f}.")

        if sig == "buy" and not held:
            gross = sum(p["qty"] * prices[k] for k, p in positions.items())
            if gross + 50 > 200:
                blocked = 1
                risk_reason = f"projected allocation ${gross+50:,.2f} exceeds total cap $200.00 [BLOCK]"
            else:
                intent, executed = "buy", 1
                qty = 50.0 / prices[s]
                positions[s] = {"qty": qty, "cost": 50.0}
                _ins("trades", ts=ts(t), loop_id=loop_id, symbol=s, asset_class=ac,
                     side="buy", qty=qty, notional=50.0, ref_price=round(prices[s], 2),
                     fill_price=round(prices[s], 2), status="shadow", shadow=1, reason=reason)
        elif sig == "buy" and held:
            pos_reason = f"already long {positions[s]['qty']:.6g} {s}; not adding."
        elif sig == "sell" and held:
            intent, executed = "sell", 1
            p = positions.pop(s)
            pnl = p["qty"] * prices[s] - p["cost"]
            realized += pnl
            _ins("trades", ts=ts(t), loop_id=loop_id, symbol=s, asset_class=ac,
                 side="sell", qty=p["qty"], notional=p["qty"] * prices[s],
                 ref_price=round(prices[s], 2), fill_price=round(prices[s], 2),
                 realized_pl=round(pnl, 4), status="shadow", shadow=1, reason=reason)
            _ins("events", ts=ts(t), loop_id=loop_id, kind="info",
                 message=f"shadow CLOSE {s} realized ${pnl:,.2f}")
        elif sig == "sell" and not held:
            pos_reason = "no open position to sell."

        _ins("decisions", ts=ts(t), loop_id=loop_id, symbol=s, asset_class=ac,
             signal=sig, intent=("hold" if blocked else intent), executed=executed,
             shadow=1, blocked=blocked, price=round(prices[s], 2),
             fast_ma=round(fast_ma[s], 4), slow_ma=round(slow_ma[s], 4),
             reason=reason, position_reason=pos_reason, risk_allowed=(0 if blocked else 1),
             risk_reason=risk_reason)

    # account snapshot
    pv = sum(p["qty"] * prices[k] for k, p in positions.items())
    invested = sum(p["cost"] for p in positions.values())
    cash = START_CASH + realized - invested
    equity = cash + pv
    day = t.date().isoformat()
    row = conn.execute("SELECT equity FROM account_snapshots WHERE ts>=? AND source='shadow' "
                       "ORDER BY ts ASC LIMIT 1", (day,)).fetchone()
    day_open = row["equity"] if row else equity
    _ins("account_snapshots", ts=ts(t), loop_id=loop_id, source="shadow",
         equity=round(equity, 2), cash=round(cash, 2), buying_power=round(cash, 2),
         positions_value=round(pv, 2), realized_pl=round(realized, 2),
         day_open_equity=round(day_open, 2), day_pl=round(equity - day_open, 2),
         day_pl_pct=round((equity - day_open) / day_open * 100, 3))
    for k, p in positions.items():
        _ins("position_snapshots", ts=ts(t), loop_id=loop_id, source="shadow", symbol=k,
             asset_class=aclass[k], qty=p["qty"], avg_entry_price=p["cost"] / p["qty"],
             current_price=round(prices[k], 2), market_value=round(p["qty"] * prices[k], 2),
             cost_basis=p["cost"], unrealized_pl=round(p["qty"] * prices[k] - p["cost"], 2),
             unrealized_pl_pct=round((p["qty"] * prices[k] - p["cost"]) / p["cost"] * 100, 2))

# a couple of illustrative events + meta
_ins("events", ts=ts(start + timedelta(minutes=30)), kind="start",
     message="agent started -- endpoint=paper execution=shadow (real orders: False)")
_ins("events", ts=ts(now - timedelta(hours=6)), kind="daily_loss_halt",
     message="daily loss limit hit: day P&L $-21.40 <= -$20.00 (recovered next day)")

db.set_meta_many(conn, {
    "agent_state": "idle", "execution_mode": "shadow", "alpaca_env": "paper",
    "shadow": 1, "will_place_real_orders": 0, "kill_switch": 0,
    "strategy": "MA crossover (fast=20, slow=50, state-based, deadband=0.15%)",
    "loop_interval_seconds": 300,
    "last_loop_at": ts(now - timedelta(minutes=3)),
    "last_loop_finished": ts(now - timedelta(minutes=3)),
    "next_loop_at": ts(now + timedelta(minutes=2)),
    "last_loop_status": "ok", "equity_market_open": 0, "account_source": "shadow",
    "open_positions": len(positions),
})
conn.commit()

acct = db.latest_account(conn)
print(f"seeded {DB}")
print(f"  {loop_id} loops, {db.counts(conn)}")
print(f"  final equity ${acct['equity']:,.2f}  realized ${realized:,.2f}  "
      f"open positions {len(positions)}")
print(f"\nview it:  DB_PATH=./data/demo.db .venv/bin/python -m dashboard")
conn.close()
