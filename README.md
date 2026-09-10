# alpaca-agent

A small, explainable 24/7 trading agent for Alpaca (stocks/ETFs + crypto), with a
live dashboard. USD base currency throughout. Built for a small, hard-capped
amount of capital as an experiment.

> **Not financial advice.** This is experimental software. The moving-average
> strategy is a mechanical rule, not a recommendation. Trade only money you can
> afford to lose. A UK-resident, non-US, non-FCA-regulated Alpaca account carries
> no UK investor protections.

## Status

| Step | What | State |
|-----|------|-------|
| 1 | `broker_alpaca.py` + `config.py` + `risk.py` (caps wired in) | done |
| 2 | SQLite schema (`db.py`) + `strategy.py` + `agent.py` loop in **shadow mode** | **done, offline selftest passes; awaiting your live paper run** |
| 3 | Dashboard (`dashboard/`) reading the same DB | **done** |
| 4 | Flip to live execution (only after you approve) | not started |
| 5 | systemd packaging + VPS deploy (shadow) | **DEPLOYED — running 24/7 on a VPS, see [DEPLOY.md](DEPLOY.md)** |

## Backtester

`backtest.py` replays the **live strategy code** (`strategy.py` + `engine.py`)
over historical bars and reports return, drawdown, trade count, win rate, fees,
and a comparison to buy-and-hold. It's a filter for bad ideas, not a forecast.

```bash
.venv/bin/python -m backtest --start 2024-01-01
.venv/bin/python -m backtest --start 2024-01-01 --symbols BTC/USD,ETH/USD \
    --timeframe 1D --ma-fast 10 --ma-slow 30 --crypto-fee-bps 25
```

Bars are cached under `data/bars_cache/`. Key finding so far: on this strategy,
**slower timeframes churn less, pay less in fees, and perform better** — daily
bars beat hourly beat 15-minute over 2025.

## Dashboard

Separate read-only Flask service. HTTP basic auth (required -- refuses to start
without `DASHBOARD_PASSWORD`). Dark "self-healing matrix" theme, polls
`/api/state` every `DASHBOARD_POLL_SECONDS`. Shows: account summary (portfolio
value, cash, day P&L, all-time P&L), portfolio-value chart, **per-symbol price +
fast/slow-MA mini-charts**, positions (stocks + crypto), live trades feed with
reasoning, decision log (trade **and** no-trade), agent status, events. The
whole page shifts **green when up / red when down**, and an "agent mind" ticker
scrolls the latest reasoning across the top.

```bash
.venv/bin/python -m dashboard                              # dev server :8080
DB_PATH=./data/demo.db .venv/bin/python -m dashboard       # view seeded demo data

.venv/bin/python -m scripts.seed_demo    # writes fake data to data/demo.db only
```

Production (step 5): `gunicorn -c deploy/gunicorn.conf.py dashboard.app:app`.

## Deployment

`deploy/` holds two systemd units (`alpaca-agent`, `alpaca-dashboard` — restart
on crash + boot, independent of each other), a gunicorn config, and `setup.sh`
(idempotent server bootstrap + updater). Full walkthrough — provision a droplet,
push to GitHub, run setup — in **[DEPLOY.md](DEPLOY.md)**.

## Step 2 check — run these

```bash
.venv/bin/python -m scripts.offline_selftest     # no keys needed; must say ALL PASS
.venv/bin/python -m agent --once                 # one real loop vs paper, shadow mode
.venv/bin/python -m scripts.show                 # view what it recorded
```

`agent --once` runs a single loop against your paper account, applies the
strategy, and writes decisions/shadow-trades to `data/agent.db`. It places **no
orders** (shadow mode). `scripts.show` is a CLI stand-in for the dashboard.

To run continuously: `.venv/bin/python -m agent` (Ctrl-C to stop).

## Layout

```
config.py          env-var config, single source of truth (nothing else reads os.environ)
models.py          plain dataclasses shared everywhere (no Alpaca types leak out)
broker_alpaca.py   Alpaca SDK wrapper: account, positions, prices, bars, clock, orders
strategy.py        MA crossover (state-based + whipsaw deadband); every signal has a reason
risk.py            hard rails: per-trade cap, total-allocation cap, daily-loss halt, kill switch
shadow.py          simulated portfolio for shadow mode (fills, positions, P&L)
db.py              SQLite (WAL) -- shared source of truth for agent + dashboard
agent.py           the loop:  kill switch -> snapshot -> daily-loss -> per-symbol decide/act
scripts/smoke_test.py       read-only Alpaca connectivity check (no orders)
scripts/offline_selftest.py  6-scenario agent test, no keys needed
scripts/show.py             CLI view of the DB (dashboard stand-in until step 3)
scripts/set_keys.py         interactive helper to put keys in .env
```

## How shadow mode works

`EXECUTION_MODE=shadow`: `submit_market_order()` raises, and the agent never
calls it. Instead, when strategy + risk would trade, it books a **simulated**
fill against a portfolio that starts with `SHADOW_STARTING_CASH` USD. Positions,
fills and P&L are persisted so the dashboard shows real behaviour. Zero funds,
zero orders.

## Setup

Requires Python 3.9+ (VPS: 3.11 recommended).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env: paste your Alpaca PAPER keys, leave ALPACA_ENV=paper, EXECUTION_MODE=shadow
```

## Step 1 check — run this

```bash
.venv/bin/python -m scripts.smoke_test
```

It prints your account, positions, live prices, bars, recent orders, and a risk
dry-run. **It never places an order.** If that looks right, tell me and I'll
start step 2.

## Safety model

- **`ALPACA_ENV`** (`paper`/`live`) — which endpoint. `live` also requires
  `ALPACA_ALLOW_LIVE=true` or the app won't start.
- **`EXECUTION_MODE`** (`shadow`/`live`) — independent switch. `shadow` = strategy
  runs and logs decisions, `submit_market_order()` raises if called.
- **Kill switch** — set `KILL_SWITCH=true` or `touch ./KILL`; checked at the top
  of every loop iteration.
- Real orders happen **only** when `ALPACA_ENV=live` **and** `EXECUTION_MODE=live`.
