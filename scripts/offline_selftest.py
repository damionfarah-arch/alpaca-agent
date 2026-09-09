"""Offline end-to-end test of the shadow-mode agent loop -- NO Alpaca, NO keys.

Stubs the broker with deterministic data and runs `AgentLoop.run_once()` against
a throwaway SQLite DB, then asserts decision / trade / shadow-portfolio / halt
rows are what we expect.

    .venv/bin/python -m scripts.offline_selftest
"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta

_TMP = tempfile.mkdtemp(prefix="alpaca-selftest-")
os.environ.update(
    ALPACA_API_KEY="offline",
    ALPACA_SECRET_KEY="offline",
    ALPACA_ENV="paper",
    EXECUTION_MODE="shadow",
    SHADOW_STARTING_CASH="1000",
    EQUITY_SYMBOLS="SPY",
    CRYPTO_SYMBOLS="BTC/USD",
    MA_FAST="3",
    MA_SLOW="5",
    MAX_POSITION_NOTIONAL_USD="50",
    MAX_TOTAL_ALLOCATION_USD="200",
    MAX_DAILY_LOSS_USD="20",
    DB_PATH=os.path.join(_TMP, "selftest.db"),
    KILL_SWITCH_FILE=os.path.join(_TMP, "KILL"),
    KILL_SWITCH="false",
)

import agent as agent_mod  # noqa: E402
import db  # noqa: E402
from models import AccountSnapshot, AssetClass, Bar, ClockInfo, utcnow  # noqa: E402

FAIL = 0


def check(name: str, cond: bool, extra="") -> None:
    global FAIL
    if not cond:
        FAIL += 1
    tail = f" -- {extra}" if extra != "" else ""
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{tail}")


def _bars(closes):
    t0 = utcnow() - timedelta(hours=len(closes))
    return [Bar(t0 + timedelta(hours=i), c, c, c, c, 10.0) for i, c in enumerate(closes)]


BULLISH = [100, 99, 98, 97, 96, 95, 96, 98, 101, 105, 110]
BEARISH = [90, 92, 94, 96, 98, 100, 99, 96, 92, 88, 83]


class FakeBroker:
    def __init__(self, *, bullish=True, market_open=True, price=100.0):
        self._bullish = bullish
        self._market_open = market_open
        self._price = price
        self.submitted = []

    def asset_class_of(self, symbol):
        return AssetClass.CRYPTO if "/" in symbol else AssetClass.EQUITY

    def normalize_symbol(self, symbol, asset_class=None):
        return symbol

    def get_account(self):
        return AccountSnapshot(100000, 100000, 400000, "USD", 100000, "ACTIVE")

    def get_positions(self):
        return []

    def get_clock(self):
        now = utcnow()
        return ClockInfo(now, self._market_open, now + timedelta(hours=1),
                         now + timedelta(hours=7))

    def get_prices(self, symbols):
        return {s: self._price for s in symbols}

    def get_bars(self, symbol, timeframe=None, lookback=None):
        return _bars(BULLISH if self._bullish else BEARISH)

    def submit_market_order(self, *a, **k):
        self.submitted.append((a, k))
        raise AssertionError("shadow-mode selftest must never submit an order")


def _wipe_db():
    base = os.environ["DB_PATH"]
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(base + suffix)
        except FileNotFoundError:
            pass


def run_loop(fake, *, seed_shadow=None):
    _wipe_db()
    agent_mod.AlpacaBroker = lambda *a, **k: fake
    loop = agent_mod.AgentLoop()
    if seed_shadow:
        for sym, ac, qty, price in seed_shadow:
            db.open_shadow_position(loop.conn, loop_id=0, symbol=sym,
                                    asset_class=ac, qty=qty, price=price)
    loop.run_once()
    return loop.conn


print("\n=== offline shadow-mode selftest ===\n")

# --- 1. bullish + flat -> shadow BUY opens a simulated position -------------
print("scenario 1: bullish + flat")
fake = FakeBroker(bullish=True, market_open=True, price=110.0)
conn = run_loop(fake)
by_sym = {d["symbol"]: d for d in db.recent_decisions(conn, 10)}
check("BTC signal == buy", by_sym["BTC/USD"]["signal"] == "buy")
check("BTC intent == buy", by_sym["BTC/USD"]["intent"] == "buy")
check("BTC decision marked shadow", by_sym["BTC/USD"]["shadow"] == 1)
check("no real order submitted", fake.submitted == [])
sp = {p["symbol"]: p for p in db.get_shadow_positions(conn)}
check("shadow position opened for BTC/USD", "BTC/USD" in sp)
check("shadow qty ~= 50/110", "BTC/USD" in sp and abs(sp["BTC/USD"]["qty"] - 50/110) < 1e-6,
      sp.get("BTC/USD", {}).get("qty"))
trades = db.recent_trades(conn, 10)
check("shadow buy trade recorded with fill price",
      any(t["side"] == "buy" and t["fill_price"] == 110.0 and t["shadow"] == 1 for t in trades))
acct = db.latest_account(conn)
check("account snapshot source == shadow", acct["source"] == "shadow", acct["source"])
check("shadow equity ~ starting cash", abs(acct["equity"] - 1000.0) < 1.0,
      f"{acct['equity']:.2f}")
conn.close()

# --- 2. bullish + already holding (seeded) -> HOLD, no new trade -----------
print("\nscenario 2: bullish + already holding BTC (shadow book)")
fake2 = FakeBroker(bullish=True, market_open=True, price=110.0)
conn = run_loop(fake2, seed_shadow=[("BTC/USD", "crypto", 0.01, 100.0)])
btc = next(d for d in db.recent_decisions(conn, 10) if d["symbol"] == "BTC/USD")
check("BTC signal still buy", btc["signal"] == "buy")
check("BTC intent == hold (already long)", btc["intent"] == "hold", btc["intent"])
check("position_reason mentions long", "long" in (btc["position_reason"] or "").lower())
check("no new BTC buy trade",
      not any(t["side"] == "buy" and t["symbol"] == "BTC/USD"
              for t in db.recent_trades(conn, 10)))
sp = {p["symbol"]: p for p in db.get_shadow_positions(conn)}
check("BTC shadow position unchanged (qty 0.01)",
      "BTC/USD" in sp and abs(sp["BTC/USD"]["qty"] - 0.01) < 1e-9)
conn.close()

# --- 3. bearish + holding -> shadow CLOSE with realized P&L ----------------
print("\nscenario 3: bearish + holding BTC -> exit")
fake3 = FakeBroker(bullish=False, market_open=True, price=120.0)  # up from entry 100
conn = run_loop(fake3, seed_shadow=[("BTC/USD", "crypto", 0.5, 100.0)])
btc = next(d for d in db.recent_decisions(conn, 10) if d["symbol"] == "BTC/USD")
check("BTC signal == sell", btc["signal"] == "sell", btc["signal"])
check("BTC intent == sell", btc["intent"] == "sell", btc["intent"])
check("shadow position closed", db.get_shadow_positions(conn) == [])
sell = next(t for t in db.recent_trades(conn, 10) if t["side"] == "sell")
check("realized P&L == (120-100)*0.5 == 10", abs(sell["realized_pl"] - 10.0) < 1e-6,
      str(sell["realized_pl"]))
check("total shadow realized P&L == 10", abs(db.shadow_realized_pl(conn) - 10.0) < 1e-6)
conn.close()

# --- 4. kill-switch file -> halt --------------------------------------------
print("\nscenario 4: kill-switch file present")
open(os.environ["KILL_SWITCH_FILE"], "w").close()
conn = run_loop(FakeBroker())
os.remove(os.environ["KILL_SWITCH_FILE"])
last = db.latest_loop(conn)
check("loop status == halted", last["status"] == "halted", last["status"])
check("halt_reason mentions kill", "kill" in (last["halt_reason"] or "").lower())
check("kill_switch event logged",
      any(e["kind"] == "kill_switch" for e in db.recent_events(conn, 20)))
check("no decisions recorded during halt", db.recent_decisions(conn, 5) == [])
conn.close()

# --- 5. daily-loss halt ----------------------------------------------------
print("\nscenario 5: daily loss limit breached -> halt")
_wipe_db()
agent_mod.AlpacaBroker = lambda *a, **k: FakeBroker(bullish=True, market_open=True, price=50.0)
loop = agent_mod.AgentLoop()
# earlier snapshot today: day opened at $1000 equity
db.record_account(loop.conn, 0, AccountSnapshot(1000, 1000, 1000, "USD", 1000, "SHADOW"),
                  source="shadow", day_open_equity=1000, day_pl=0, day_pl_pct=0)
# seed a position: cost 2000, now worth 1000 -> shadow equity ~ 0, day P&L ~ -1000
db.open_shadow_position(loop.conn, loop_id=0, symbol="BTC/USD", asset_class="crypto",
                        qty=20.0, price=100.0)
loop.run_once()
last = db.latest_loop(loop.conn)
check("loop halted on daily loss", last["status"] == "halted", last["status"])
check("halt reason mentions daily loss",
      "daily loss" in (last["halt_reason"] or "").lower(), last["halt_reason"])
check("daily_loss_halt event logged",
      any(e["kind"] == "daily_loss_halt" for e in db.recent_events(loop.conn, 20)))
loop.conn.close()

# --- 6. equity market closed -> SPY skipped, BTC still trades --------------
print("\nscenario 6: equity market closed")
conn = run_loop(FakeBroker(bullish=True, market_open=False, price=110.0))
d = {x["symbol"]: x for x in db.recent_decisions(conn, 10)}
check("SPY skipped (market closed)", "closed" in d["SPY"]["reason"].lower())
check("BTC still evaluated", d["BTC/USD"]["signal"] == "buy")
conn.close()

print(f"\n=== {'ALL PASS' if FAIL == 0 else str(FAIL) + ' FAILURE(S)'} ===\n")
raise SystemExit(1 if FAIL else 0)
