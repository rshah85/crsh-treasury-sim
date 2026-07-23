# CRSH Treasury Simulator & Live Trading Agent

CRSH is a pari-mutuel prediction market. This repo has two parts:

- **A treasury simulator** (`/backend` + `/frontend`) that backtests a contrarian
  betting strategy — seed both sides of every market, then lean into the minority
  side as crowd imbalance grows — to see whether it harvests real edge over many
  simulated markets.
- **A live trading agent** (`/agent`) that runs that same validated strategy
  against a real on-chain market: poll pool sizes every second, detect 70/30+
  imbalance, size a contrarian bet with the Kelly criterion, and fire it in a
  narrow window before the market closes — with a kill switch, exposure caps,
  and a circuit breaker, since it's meant to eventually move real USDC.

The agent reuses the simulator's sizing math directly (`backend/sim_v2.py`)
rather than reimplementing it — see [Live trading agent](#live-trading-agent)
below for how.

## Repo layout

```
backend/    Simulation engines (sim.py, sim_v2.py) behind a FastAPI service
frontend/   React dashboard for running simulations and viewing results
agent/      Live trading daemon — decision engine, risk controls, chain adapter
docs/       Design notes for the live agent (latency modeling, graduation gate)
```

---

## Backend: simulation engines

Two engines live side by side:

- **`sim.py` (v1)** — the original model: symmetric seed both sides, contrarian
  overlay sized as a fixed convex fraction of imbalance, sophistication decay
  over the run.
- **`sim_v2.py` (v2)** — five structural improvements over v1, and the engine the
  live agent's decision logic is built on:
  1. **Kelly-sized contrarian bets** (`_kelly_bet_size`) instead of a fixed
     fraction.
  2. **Daily market priority scoring** — capital deploys to the
     highest-imbalance markets first when the daily budget is tight.
  3. **3-phase aggression schedule** (`_get_phase`) — the run is split into
     thirds; phase 1 is most aggressive (low threshold, full Kelly), phase 3 is
     most selective (high threshold, half Kelly).
  4. **Rolling daily capital budget with carryover** — unused budget rolls to
     the next day, capped.
  5. **Volume-weighted rake harvesting** — once cumulative crowd volume crosses
     a trigger, the engine stops taking contrarian positions and runs
     rake-only.

Both engines are deterministic given a `seed`, so parameter sweeps are
reproducible.

### Running the backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Endpoints:

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| POST | `/api/simulate` | Run the v1 engine; body is `SimRequest` (see `main.py`) |
| POST | `/api/simulate/both` | Run v1 and v2 with shared params, return both |
| POST | `/api/sensitivity` | 2D sweep over `edge_discount × sophistication_decay`, returns a return-% grid for a heatmap |

## Frontend

```bash
cd frontend
npm install
npm run dev
```

Calls the backend at `http://localhost:8000` by default; override with
`VITE_API_URL` (see `frontend/.env`). Lets you adjust simulation parameters and
re-run live; shows NAV over time, per-day fee income vs. contrarian edge, the
per-market P&L distribution, and summary stats for both engines side by side.

---

## Live trading agent

`agent/` is an `asyncio` daemon that runs the contrarian strategy against a real
(eventually) on-chain market instead of simulated data. It deliberately reuses
`sim_v2.py`'s validated math rather than re-deriving it:

- `agent/_sim_engine.py` adds `backend/` to `sys.path` (it isn't a package) and
  re-exports `SimConfigV2`, `_get_phase`, and `_kelly_bet_size` so every other
  agent module imports the same functions the simulator's backtest already
  exercised.
- `agent/launch_clock.py` bridges the gap between sim_v2's backtest-timeline
  concept of "day N of a 30-day run" and a live daemon that only has wall-clock
  time: `LaunchClock` turns `time.time() - launch_ts` into a wrapped
  `days_since_launch % 30`, and feeds that SAME wrapped value into both the
  phase calculation and the effective-edge formula so they can't silently
  diverge after one 30-day cycle (they used to — phase would wrap back to full
  aggression every 30 days while the edge estimate collapsed toward zero
  forever, a bug only visible past the horizon sim_v2's own backtest ever ran).
- `agent/decision.py` is the actual decision engine: given live pool sizes, it
  checks for 70/30+ imbalance, applies the phase-appropriate threshold, and
  sizes the bet with `_kelly_bet_size` — the same function, same formula, same
  caps as the simulator.

### Architecture

```
agent/
  chain_adapter.py       ChainAdapter interface + PlaceholderChainAdapter
  web3_chain_adapter.py  Real ChainAdapter impl, backed by the deployed contract
  erc20_abi.py           Minimal standard ERC20 ABI subset (decimals/allowance/approve)
  usdc-pool-abi.json     The real UsdcPoolPredictionMarket contract ABI
  rpc_client.py          Shared rate-limited client (token bucket + semaphore)
  decision.py            Kelly sizing + imbalance/phase logic (wraps sim_v2)
  launch_clock.py        Wall-clock -> phase/effective_edge bridge
  risk_guard.py          Exposure caps, circuit breaker, restart reconciliation
  key_custody.py         Encrypted keystore signer loading
  ledger.py              In-memory NAV / realized P&L bookkeeping
  pnl.py                 Pari-mutuel payout math for a single resolved bet
  perf_log.py            Per-bet structured JSONL log
  dashboard.py           Live rich terminal dashboard
  backtest.py            Replays sim_v2-shaped data through decision.py/risk_guard.py
  daemon.py              Orchestration + CLI entrypoint
```

**ChainAdapter** is a pinned 4-method interface
(`discover_markets`, `get_pool_sizes`, `get_close_timestamp`, `place_bet`) plus
two read-only bolt-ons needed for safety bookkeeping (`get_tx_status`,
`get_market_outcome`). Two implementations exist behind it:

- `PlaceholderChainAdapter` — simulates plausible pool dynamics, tx outcomes,
  and market resolution. Used by `--backtest`, dashboard/demo runs, and
  whenever the real chain isn't configured (see below).
- `Web3ChainAdapter` — backed by `web3.py` against the real, deployed
  `UsdcPoolPredictionMarket` contract on Monad. `daemon.py` uses this instead of
  the placeholder only when `CRSH_RPC_URL`, `CRSH_CONTRACT_ADDRESS`, and a
  keystore are ALL explicitly configured — see [Configuration](#configuration).
  A few integration decisions worth knowing:
  - **Approve once at startup, not per-bet.** The contract pulls USDC via a
    standard ERC20 allowance; the adapter checks/tops it up once in `connect()`
    so every live `bet()` call during the firing window is a single
    transaction, not approve-then-bet.
  - **That approval is bounded to `lifetime_cap_usdc`**, not "infinite
    approve" — the on-chain allowance itself can't let the signer move more
    than RiskGuard's own hard ceiling, even in a hypothetical RiskGuard bug.
  - **Gas limit is fixed config, not estimated per-bet** — avoids an extra RPC
    round trip in the firing window. Needs real calibration on testnet (see
    `docs/graduation_gate.md`).
  - **Status enum ordinals are an assumption, not a verified fact** — the ABI
    doesn't carry the enum's integer values. Flagged clearly in
    `web3_chain_adapter.py` and as a required testnet verification step in
    `docs/graduation_gate.md`.
  - **Market discovery is a full range-scan** (`0..nextMarketId()-1`) since
    this ABI has no batch "list active markets" call — fine at current volume,
    flagged as a follow-on once it grows.

Nothing in `decision.py`, `risk_guard.py`, or `daemon.py` depends on anything
beyond the pinned interface, regardless of which implementation is wired in.

**RiskGuard** is the safety-critical, persisted state: a per-bet exposure
ceiling AND a cumulative lifetime cap (both required — a per-bet-only cap
leaves many-small-bets exposure unbounded), a circuit breaker that trips on N
consecutive failures (a revert or an unconfirmed-by-close tx both count), and
restart reconciliation (a bet is marked "in flight" before its tx is submitted,
so a daemon crash between submit and record gets resolved against chain state
on the next startup instead of silently double-betting or losing the exposure
record). All of it is written by exactly one dedicated writer task via an
internal `asyncio.Queue` — per-market polling tasks send mutation requests,
never write the state file directly — so concurrent tasks can't corrupt it.

**The kill switch** is a flag file checked every tick; any error reading it
(permissions, disk, transient I/O) is treated identically to the switch being
triggered — it fails safe, never open.

### Running it

From the repo root, in any Python 3.9+ environment:

```bash
pip install -r agent/requirements.txt
python -m agent.daemon                  # live daemon, JSON event log to stdout
python -m agent.daemon --dashboard      # same, plus a live rich terminal dashboard
python -m agent.daemon --backtest       # validate decision.py/risk_guard.py against sim_v2-shaped data
python -m agent.daemon --backtest --days 90 --seed 123
```

The agent only needs `agent/requirements.txt` (`eth-account`, `rich`) — it
never imports FastAPI/pydantic, since `sim_v2.py` itself has zero third-party
dependencies.

**Dashboard mode** takes over the terminal with a live view of active markets
(pool splits, majority %, time-to-close), open positions, current phase, NAV,
and realized P&L, refreshed every second. Because it owns the terminal, the
structured JSON event log is automatically redirected to
`logs/daemon_events.jsonl` instead of stdout in this mode.

**Backtest mode** does NOT run sim_v2's own internal Kelly/PnL code — it
regenerates markets using the same distributions sim_v2 uses, then drives each
one through the real `decision.py` + `risk_guard.py`, so it validates the
actual live pipeline (phase wiring, the firing-window rule, exposure caps, the
circuit breaker) rather than re-confirming sim_v2's math in isolation. It
prints a summary (bets fired, win rate, final NAV, skip-reason breakdown) and
writes the same structured per-bet log as a live run.

**Every fired bet** is logged to `logs/performance.jsonl` as two append-only
JSON lines — `bet_placed` (market, pool split at bet time, Kelly-sized amount,
side) and `bet_resolved` once the outcome is known (win/loss, PnL, running
total) — joined by `market_id`. Two lines instead of rewriting one avoids the
same concurrent-write risk `RiskGuard`'s single-writer queue exists to prevent.

### Configuration

All of `agent/config.py`'s `AgentConfig` can be overridden via environment
variables (see `AgentConfig.from_env`): `CRSH_POLL_INTERVAL_S`,
`CRSH_FIRE_WINDOW_HIGH_S` / `_LOW_S`, `CRSH_PER_BET_CAP_USDC`,
`CRSH_LIFETIME_CAP_USDC`, `CRSH_CIRCUIT_BREAKER_THRESHOLD`,
`CRSH_STALL_THRESHOLD`, `CRSH_KILL_SWITCH_PATH`, `CRSH_RISK_GUARD_STATE_PATH`,
`CRSH_KEYSTORE_PATH`, `CRSH_PASSPHRASE_ENV_VAR`, `CRSH_PERFORMANCE_LOG_PATH`,
`CRSH_DAEMON_LOG_PATH`.

The exposure-cap and circuit-breaker defaults are conservative placeholders,
**not** production values — see `agent/config.py`'s docstring and
`docs/graduation_gate.md`.

**Real chain wiring** (`Web3ChainAdapter`) is opt-in via three more env vars,
none of which have a hardcoded default on purpose (see `agent/config.py`'s
docstring — a baked-in default here could silently flip the daemon into
live-trading mode):

```
CRSH_RPC_URL=https://rpc.monad.xyz
CRSH_CONTRACT_ADDRESS=0x0A2fbF2Dbe08880D8D0d7f2F34B4CC3761f729b1
CRSH_KEYSTORE_PATH=/path/to/keystore.json   # plus CRSH_SIGNER_PASSPHRASE
```

`daemon.py` only uses the real adapter when `CRSH_RPC_URL`,
`CRSH_CONTRACT_ADDRESS`, and `CRSH_KEYSTORE_PATH` are ALL set — otherwise it
falls back to `PlaceholderChainAdapter`, same as before this integration
existed. Also relevant: `CRSH_APPROVE_TARGET_USDC` (defaults to
`lifetime_cap_usdc` if unset), `CRSH_BET_GAS_LIMIT` / `CRSH_APPROVE_GAS_LIMIT`,
`CRSH_TX_CONFIRMATION_TIMEOUT_S` / `CRSH_TX_POLL_INTERVAL_S`, `CRSH_ABI_PATH`
(defaults to `agent/usdc-pool-abi.json`).

### Where this stands

The agent runs end-to-end against `PlaceholderChainAdapter` by default. The
real contract ABI and address have arrived and `Web3ChainAdapter` implements
the full interface against them (see above) — but going to testnet/mainnet
still requires: a confirmed gas-sponsorship mechanism, confirmed
exposure-cap/circuit-breaker values, Status-enum ordinals verified against the
live contract, and calibrated gas limits — none of which are decided/verified
yet. See `docs/graduation_gate.md` for the full placeholder → testnet →
mainnet criteria (updated with what the real ABI resolved and what it newly
requires), and `docs/queuing_latency_model.md` for the T-4s firing floor
analysis (also updated — the real adapter's `place_bet` costs more RPC calls
than the placeholder-based estimate assumed).

## Notes

- Both simulation engines are deterministic given a `seed`.
- `TODOS.md` tracks known, non-blocking gaps (currently: Kelly sizing doesn't
  account for a contrarian bet's own price impact on pool depth — untested
  against real Monad pool data, not a v1 blocker).
