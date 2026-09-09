"""Quick CLI view of what the agent has recorded -- a stand-in for the dashboard
until step 3. Read-only.

    .venv/bin/python -m scripts.show
"""

from __future__ import annotations

import db
from config import CONFIG


def main() -> int:
    conn = db.connect(CONFIG.db_path, read_only=True)

    meta = db.get_all_meta(conn)
    print("\n=== AGENT STATUS ===")
    for key in (
        "agent_state", "execution_mode", "alpaca_env", "shadow",
        "will_place_real_orders", "kill_switch", "last_loop_at",
        "last_loop_finished", "next_loop_at", "strategy",
    ):
        if key in meta:
            print(f"  {key:<24} {meta[key]}")

    acct = db.latest_account(conn)
    print("\n=== ACCOUNT (last snapshot) ===")
    if acct:
        print(f"  equity          ${acct['equity']:,.2f}")
        print(f"  cash            ${acct['cash']:,.2f}")
        print(f"  positions value ${acct['positions_value']:,.2f}")
        print(f"  day P&L         ${acct['day_pl'] or 0:,.2f} "
              f"({acct['day_pl_pct'] or 0:+.2f}%)")
    else:
        print("  (no snapshots yet)")

    print("\n=== POSITIONS (last snapshot) ===")
    positions = db.latest_positions(conn)
    if not positions:
        print("  (none)")
    for p in positions:
        print(f"  {p['symbol']:<10} {p['asset_class']:<9} qty={p['qty']:<12g} "
              f"@ ${p['current_price']:,.2f}  mv=${p['market_value']:,.2f}  "
              f"uPL ${p['unrealized_pl']:,.2f} ({p['unrealized_pl_pct']:+.2f}%)")

    print("\n=== RECENT DECISIONS ===")
    for d in db.recent_decisions(conn, 20):
        flags = []
        if d["executed"]:
            flags.append("EXECUTED")
        elif d["shadow"] and d["intent"] != "hold":
            flags.append("SHADOW")
        if d["blocked"]:
            flags.append("BLOCKED")
        tag = f"  [{'/'.join(flags)}]" if flags else ""
        ts = d["ts"][11:19]
        print(f"  {ts}  {d['symbol']:<9} sig={d['signal']:<4} -> {d['intent']:<4}{tag}")
        print(f"            {d['reason']}")
        if d["risk_reason"]:
            print(f"            risk: {d['risk_reason']}")

    print("\n=== RECENT TRADES ===")
    trades = db.recent_trades(conn, 20)
    if not trades:
        print("  (none)")
    for t in trades:
        size = (f"qty {t['qty']:g}" if t["qty"] is not None
                else f"${t['notional']:,.2f}")
        print(f"  {t['ts'][11:19]}  {t['side'].upper():<4} {t['symbol']:<9} {size:<16} "
              f"{t['status']:<10} {'(shadow)' if t['shadow'] else ''}")

    print("\n=== RECENT EVENTS ===")
    for e in db.recent_events(conn, 10):
        print(f"  {e['ts'][11:19]}  {e['kind']:<16} {e['message']}")

    conn.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
