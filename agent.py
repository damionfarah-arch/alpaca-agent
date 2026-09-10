"""Main trading loop.

    .venv/bin/python -m agent            # run forever (systemd target)
    .venv/bin/python -m agent --once     # single iteration, print summary, exit

Every loop iteration:
  1. check kill switch                    -> halt, log, sleep
  2. fetch account + positions + clock, snapshot to DB
  3. check daily-loss limit               -> halt, log, sleep
  4. for each symbol: bars -> strategy -> position logic -> risk -> (shadow log
     OR submit order) -> write a decision row (ALWAYS, trade or no-trade)
  5. update heartbeat meta, sleep until next run

In shadow mode (EXECUTION_MODE=shadow) no order is ever submitted; the loop
records what it *would* have done.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import db
from broker_alpaca import AlpacaBroker, BrokerError
from config import CONFIG
from engine import plan_trade
from models import AssetClass, Position, Side
from risk import OrderIntent, RiskManager, daily_loss_halt, kill_switch_active
from shadow import ShadowPortfolio
from strategy import BUY, HOLD, SELL, get_strategy

log = logging.getLogger("agent")

MAX_CONSECUTIVE_ERRORS = 5


def _setup_logging() -> None:
    logging.basicConfig(
        level=CONFIG.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


class AgentLoop:
    def __init__(self) -> None:
        self.cfg = CONFIG
        self.conn = db.init_db(self.cfg.db_path)
        self.broker = AlpacaBroker(self.cfg)
        self.strategy = get_strategy(self.cfg)
        self.risk = RiskManager(self.cfg)
        self.shadow = ShadowPortfolio(self.conn, self.cfg) if self.cfg.is_shadow else None
        self._stop = threading.Event()
        self._consecutive_errors = 0

        db.log_event(
            self.conn,
            "start",
            f"agent started -- {self.cfg.describe()} "
            f"(real orders: {self.cfg.will_place_real_orders})",
        )
        db.set_meta_many(
            self.conn,
            {
                "agent_pid": _pid(),
                "agent_state": "starting",
                "execution_mode": self.cfg.execution_mode,
                "alpaca_env": self.cfg.alpaca_env,
                "shadow": int(self.cfg.is_shadow),
                "will_place_real_orders": int(self.cfg.will_place_real_orders),
                "strategy": self.strategy.describe(),
                "loop_interval_seconds": self.cfg.loop_interval_seconds,
                "started_at": _now_iso(),
            },
        )
        log.info("agent init OK -- %s", self.cfg.describe())

    # ------------------------------------------------------------------ #
    def request_stop(self, *_a) -> None:
        log.info("stop requested, will exit after this iteration")
        self._stop.set()

    def _sleep(self, seconds: int) -> None:
        # interruptible sleep so systemd stop / Ctrl-C is responsive
        self._stop.wait(timeout=seconds)

    # ------------------------------------------------------------------ #
    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        log.info("entering loop (interval %ss)", self.cfg.loop_interval_seconds)
        while not self._stop.is_set():
            try:
                self.run_once()
                self._consecutive_errors = 0
            except Exception:  # noqa: BLE001 - loop must survive transient errors
                self._consecutive_errors += 1
                err = traceback.format_exc()
                log.error("loop iteration failed (%d/%d):\n%s",
                          self._consecutive_errors, MAX_CONSECUTIVE_ERRORS, err)
                db.log_event(self.conn, "error", err.strip().splitlines()[-1])
                db.set_meta(self.conn, "agent_state", "error")
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    db.log_event(self.conn, "stop",
                                 f"exiting after {MAX_CONSECUTIVE_ERRORS} consecutive errors")
                    log.critical("too many consecutive errors, exiting")
                    return 1
            if self._stop.is_set():
                break
            self._schedule_next()
            self._sleep(self.cfg.loop_interval_seconds)

        db.log_event(self.conn, "stop", "agent stopped")
        db.set_meta(self.conn, "agent_state", "stopped")
        log.info("agent stopped cleanly")
        return 0

    def _schedule_next(self) -> None:
        nxt = datetime.now(timezone.utc) + timedelta(seconds=self.cfg.loop_interval_seconds)
        db.set_meta(self.conn, "next_loop_at", nxt.isoformat())

    # ------------------------------------------------------------------ #
    def run_once(self) -> None:
        cfg = self.cfg
        loop_id = db.start_loop(self.conn, cfg.execution_mode, cfg.alpaca_env)
        db.set_meta_many(
            self.conn,
            {"last_loop_at": _now_iso(), "last_loop_id": loop_id, "agent_state": "running"},
        )
        log.info("---- loop %d ----", loop_id)

        # 1. kill switch --------------------------------------------------
        ks = kill_switch_active(cfg)
        db.set_meta(self.conn, "kill_switch", int(ks.halted))
        if ks.halted:
            log.warning("KILL SWITCH ACTIVE: %s", ks.reason)
            self._halt(loop_id, "kill_switch", ks.reason)
            return

        # 2. market clock (always from the real broker) -----------------
        try:
            clock = self.broker.get_clock()
            market_open = clock.is_open
        except BrokerError as exc:
            log.warning("clock fetch failed, assuming equities closed: %s", exc)
            clock = None
            market_open = False

        # live prices for every configured symbol -- used for marking the
        # shadow book and for simulated fills; cheap, one request per asset class
        try:
            prices = self.broker.get_prices(cfg.all_symbols)
        except BrokerError as exc:
            log.warning("price fetch failed: %s", exc)
            prices = {}

        # account + positions: real broker in live mode, simulated in shadow mode
        source = "shadow" if cfg.is_shadow else "broker"
        if self.shadow is not None:
            account = self.shadow.account(prices)
            positions = self.shadow.positions(prices)
        else:
            account = self.broker.get_account()
            positions = self.broker.get_positions()

        day = db.utc_day()
        prior_day_open = db.day_open_equity(self.conn, day, source=source)
        day_open = prior_day_open if prior_day_open is not None else account.equity
        day_pl = account.equity - day_open
        day_pl_pct = (day_pl / day_open * 100.0) if day_open else 0.0
        db.record_account(
            self.conn, loop_id, account, source=source,
            realized_pl=db.shadow_realized_pl(self.conn) if self.shadow else 0.0,
            day_open_equity=day_open, day_pl=day_pl, day_pl_pct=day_pl_pct,
        )
        db.record_positions(self.conn, loop_id, positions, source=source)
        db.set_meta_many(
            self.conn,
            {
                "equity": f"{account.equity:.2f}",
                "cash": f"{account.cash:.2f}",
                "positions_value": f"{account.positions_value:.2f}",
                "day_pl": f"{day_pl:.2f}",
                "day_pl_pct": f"{day_pl_pct:.2f}",
                "equity_market_open": int(market_open),
                "open_positions": len(positions),
                "account_source": source,
            },
        )
        log.info(
            "[%s] equity $%.2f cash $%.2f day P&L $%.2f (%.2f%%) | %d positions | equities %s",
            source, account.equity, account.cash, day_pl, day_pl_pct, len(positions),
            "OPEN" if market_open else "closed",
        )

        # 3. daily loss limit -----------------------------------------
        halt = daily_loss_halt(cfg, account=account, opening_equity=day_open)
        if halt.halted:
            log.warning("DAILY LOSS HALT: %s", halt.reason)
            self._halt(loop_id, "daily_loss_halt", halt.reason, market_open=market_open)
            return

        # 4. per-symbol evaluation ----------------------------------------
        pos_by_symbol = {p.symbol: p for p in positions}
        traded_any = False
        for symbol in cfg.all_symbols:
            try:
                traded_any |= self._evaluate_symbol(
                    loop_id, symbol, account, positions, pos_by_symbol,
                    market_open, prices,
                )
            except BrokerError as exc:
                log.error("symbol %s failed: %s", symbol, exc)
                db.record_decision(
                    self.conn, loop_id=loop_id, symbol=symbol,
                    asset_class=self.broker.asset_class_of(symbol).value,
                    signal="hold", intent="hold", executed=False,
                    shadow=cfg.is_shadow, blocked=False, price=None,
                    fast_ma=None, slow_ma=None,
                    reason=f"evaluation error: {exc}",
                    position_reason=None, risk_allowed=None, risk_reason=None,
                    order_id=None, indicators=None,
                )

        # 5. re-snapshot if anything traded, so equity/positions reflect fills
        if traded_any:
            if self.shadow is not None:
                account = self.shadow.account(prices)
                positions = self.shadow.positions(prices)
            else:
                account = self.broker.get_account()
                positions = self.broker.get_positions()
            day_pl = account.equity - day_open
            day_pl_pct = (day_pl / day_open * 100.0) if day_open else 0.0
            db.record_account(
                self.conn, loop_id, account, source=source,
                realized_pl=db.shadow_realized_pl(self.conn) if self.shadow else 0.0,
                day_open_equity=day_open, day_pl=day_pl, day_pl_pct=day_pl_pct,
            )
            db.record_positions(self.conn, loop_id, positions, source=source)
            db.set_meta_many(
                self.conn,
                {
                    "equity": f"{account.equity:.2f}",
                    "cash": f"{account.cash:.2f}",
                    "positions_value": f"{account.positions_value:.2f}",
                    "day_pl": f"{day_pl:.2f}",
                    "day_pl_pct": f"{day_pl_pct:.2f}",
                    "open_positions": len(positions),
                },
            )

        db.finish_loop(self.conn, loop_id, "ok", equity_market_open=market_open)
        db.set_meta_many(
            self.conn,
            {"agent_state": "idle", "last_loop_status": "ok", "last_loop_finished": _now_iso()},
        )
        log.info("---- loop %d done ----", loop_id)

    # ------------------------------------------------------------------ #
    def _evaluate_symbol(
        self,
        loop_id: int,
        symbol: str,
        account,
        positions: list[Position],
        pos_by_symbol: dict[str, Position],
        market_open: bool,
        prices: dict[str, float],
    ) -> bool:
        """Returns True if a trade (shadow or live) was executed for this symbol."""
        cfg = self.cfg
        asset_class = self.broker.asset_class_of(symbol)
        is_equity = asset_class == AssetClass.EQUITY
        position = pos_by_symbol.get(self.broker.normalize_symbol(symbol))
        held_qty = position.qty if position else 0.0

        # equities: skip when the market is closed (unless explicitly allowed)
        if is_equity and not market_open and not cfg.trade_equity_when_closed:
            db.record_decision(
                self.conn, loop_id=loop_id, symbol=symbol,
                asset_class=asset_class.value, signal="hold", intent="hold",
                executed=False, shadow=cfg.is_shadow, blocked=False,
                price=None, fast_ma=None, slow_ma=None,
                reason="equity market closed -- no evaluation (crypto keeps trading).",
                position_reason=None, risk_allowed=None, risk_reason=None,
                order_id=None, indicators=None,
            )
            log.info("  %-10s HOLD  (equity market closed)", symbol)
            return False

        bars = self.broker.get_bars(symbol)
        sig = self.strategy.evaluate(bars)

        # ---- position logic: translate raw signal -> intent (shared with backtest) ----
        ref_price = sig.price or (position.current_price if position else 0.0)
        planned = plan_trade(
            sig.action,
            held_qty=held_qty,
            symbol=symbol,
            notional_cap=cfg.risk.max_position_notional_usd,
            ref_price=ref_price,
        )
        intent_action = planned.intent_action
        position_reason = planned.position_reason
        order_intent = planned.order_intent

        # ---- risk evaluation ----
        risk_allowed: bool | None = None
        risk_reason: str | None = None
        blocked = False
        executed = False
        order_id = None
        decision_id = None

        if order_intent is not None:
            decision = self.risk.evaluate_order(order_intent, account, positions)
            risk_allowed = decision.allowed
            risk_reason = f"{decision.reason} [{decision.summary()}]"
            if not decision.allowed:
                blocked = True
                intent_action = HOLD
                log.warning("  %-10s %s BLOCKED: %s", symbol, order_intent.side.value,
                            decision.reason)

        # ---- record the decision (always) ----
        decision_id = db.record_decision(
            self.conn, loop_id=loop_id, symbol=symbol, asset_class=asset_class.value,
            signal=sig.action,
            intent=intent_action if not blocked else "hold",
            executed=False,  # updated below if an order is placed
            shadow=cfg.is_shadow, blocked=blocked,
            price=sig.price, fast_ma=sig.fast_ma, slow_ma=sig.slow_ma,
            reason=sig.reason, position_reason=position_reason,
            risk_allowed=risk_allowed, risk_reason=risk_reason,
            order_id=None, indicators=sig.indicators,
        )

        # ---- act on an allowed intent ----
        if order_intent is not None and not blocked:
            fill_px = prices.get(self.broker.normalize_symbol(symbol)) or sig.price or 0.0
            if cfg.is_shadow:
                assert self.shadow is not None
                if order_intent.side == Side.BUY:
                    qty = self.shadow.open(
                        loop_id=loop_id, symbol=self.broker.normalize_symbol(symbol),
                        asset_class=asset_class, notional=order_intent.notional_usd,
                        price=fill_px,
                    )
                    db.record_trade(
                        self.conn, loop_id=loop_id, decision_id=decision_id,
                        symbol=symbol, asset_class=asset_class.value, side="buy",
                        qty=qty, notional=order_intent.notional_usd,
                        ref_price=sig.price, fill_price=fill_px,
                        status="shadow", shadow=True, order_id=None,
                        reason=sig.reason,
                    )
                else:  # SELL -- close the simulated position
                    qty, realized = self.shadow.close(
                        symbol=self.broker.normalize_symbol(symbol), price=fill_px
                    )
                    db.record_trade(
                        self.conn, loop_id=loop_id, decision_id=decision_id,
                        symbol=symbol, asset_class=asset_class.value, side="sell",
                        qty=qty, notional=qty * fill_px,
                        ref_price=sig.price, fill_price=fill_px,
                        realized_pl=realized,
                        status="shadow", shadow=True, order_id=None,
                        reason=sig.reason,
                    )
                    db.log_event(
                        self.conn, "info",
                        f"shadow CLOSE {symbol} realized ${realized:,.2f}",
                        loop_id=loop_id,
                    )
                log.warning("  %-10s SHADOW %s  %s  @ $%.2f (simulated fill)",
                            symbol, order_intent.side.value,
                            _size_str(order_intent), fill_px)
                return True
            else:
                order = self.broker.submit_market_order(
                    symbol,
                    order_intent.side,
                    qty=order_intent.qty if order_intent.side == Side.SELL else None,
                    notional=(
                        round(order_intent.notional_usd, 2)
                        if order_intent.side == Side.BUY else None
                    ),
                    client_order_id=f"agent-{loop_id}-{symbol.replace('/', '')}-{int(time.time())}",
                )
                executed = True
                order_id = order.id
                db.record_trade(
                    self.conn, loop_id=loop_id, decision_id=decision_id,
                    symbol=symbol, asset_class=asset_class.value,
                    side=order_intent.side.value,
                    qty=order.qty, notional=order.notional,
                    ref_price=sig.price, fill_price=order.filled_avg_price,
                    status=order.status, shadow=False, order_id=order.id,
                    reason=sig.reason,
                )
                self.conn.execute(
                    "UPDATE decisions SET executed=1, order_id=? WHERE id=?",
                    (order_id, decision_id),
                )
                self.conn.commit()
                db.log_event(
                    self.conn, "info",
                    f"ORDER {order_intent.side.value} {symbol} {_size_str(order_intent)} "
                    f"-> {order.status} ({order.id})",
                    loop_id=loop_id,
                )
                log.warning("  %-10s ORDER %s %s -> %s", symbol,
                            order_intent.side.value, _size_str(order_intent), order.status)
                return True
        else:
            reason_tail = position_reason or sig.reason
            log.info("  %-10s HOLD  (%s)", symbol, _short(reason_tail))
        return False

    # ------------------------------------------------------------------ #
    def _halt(
        self, loop_id: int, kind: str, reason: str, *, market_open: bool | None = None
    ) -> None:
        # dedupe: only log the event on transition
        last = db.get_meta(self.conn, "agent_state")
        state = f"halted_{kind}"
        if last != state:
            db.log_event(self.conn, kind, reason, loop_id=loop_id)
        db.finish_loop(self.conn, loop_id, "halted", halt_reason=reason,
                       equity_market_open=market_open)
        db.set_meta_many(
            self.conn,
            {"agent_state": state, "last_loop_status": "halted",
             "halt_reason": reason, "last_loop_finished": _now_iso()},
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def _pid() -> int:
    import os

    return os.getpid()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _size_str(intent: OrderIntent) -> str:
    if intent.qty is not None:
        return f"qty {intent.qty:g} (~${intent.notional_usd:,.2f})"
    return f"${intent.notional_usd:,.2f} notional"


def _short(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpaca trading agent loop")
    parser.add_argument("--once", action="store_true",
                        help="run a single iteration then exit")
    args = parser.parse_args(argv)

    _setup_logging()
    if CONFIG.will_place_real_orders:
        log.warning("!!! EXECUTION_MODE=live AND ALPACA_ENV=live -- REAL MONEY !!!")

    agent = AgentLoop()
    try:
        if args.once:
            agent.run_once()
            _print_once_summary(agent)
            return 0
        return agent.run_forever()
    finally:
        agent.close()


def _print_once_summary(agent: AgentLoop) -> None:
    conn = agent.conn
    acct = db.latest_account(conn)
    print("\n=== single loop complete ===")
    if acct:
        print(f"equity ${acct['equity']:,.2f}  cash ${acct['cash']:,.2f}  "
              f"day P&L ${acct['day_pl'] or 0:,.2f}")
    print("\nlast decisions:")
    for d in db.recent_decisions(conn, limit=len(CONFIG.all_symbols)):
        flags = []
        if d["blocked"]:
            flags.append("BLOCKED")
        if d["shadow"] and d["intent"] != "hold":
            flags.append("SHADOW")
        if d["executed"]:
            flags.append("EXECUTED")
        tag = f" [{'/'.join(flags)}]" if flags else ""
        print(f"  {d['symbol']:<10} signal={d['signal']:<4} intent={d['intent']:<4}{tag}")
        print(f"      {d['reason']}")
        if d["risk_reason"]:
            print(f"      risk: {d['risk_reason']}")


if __name__ == "__main__":
    sys.exit(main())
