"""
Backtest mode (--backtest) — replays sim_v2-shaped synthetic markets through the
REAL agent decision + risk pipeline (decision.py, risk_guard.py), not sim_v2's own
internal Kelly/PnL math.

The point: validate THIS repo's live decision engine and RiskGuard's safety
enforcement — phase/LaunchClock wiring, the firing-window rule, exposure caps, the
circuit breaker — against the same statistical distributions sim_v2's own backtest
already exercises. A plain `run_simulation_v2()` run can't catch integration bugs
here, because it never calls decision.py or risk_guard.py at all; this does.

What's reused from sim_v2 vs. replaced:
  - REUSED: SimConfigV2's market-generation parameters and the exact RNG-driven
    market population formula from `_simulate_day_v2`'s generation block (cluster
    biases, per-market fan_bias/crowd_vol, pool seeding with seed_per_side).
  - REPLACED: sim_v2's own `_kelly_bet_size` call site, its day-loop phase/edge
    plumbing, its daily capital budget/carryover, and its PnL bookkeeping — all
    of that is exactly what decision.py, LaunchClock, RiskGuard, and agent/pnl.py
    already do for the live agent, so this drives THOSE instead of duplicating them.
  - NOT reused: sim_v2's priority-sort-by-bias market ordering and daily capital
    budget. Neither exists in the live agent — decision.py has no capital-budget
    concept, and the live daemon processes markets in poll/arrival order, not
    priority order. Reusing the sort here would validate a processing order the
    live agent never actually uses.
  - NOT reused: sim_v2's volume-weighted rake-harvest mode. decision.py doesn't
    implement it either (out of scope for the v1 live agent per the design doc),
    so a faithful backtest of the live pipeline must not gate on it either.

There's no live wall clock in a backtest, so LaunchClock is driven by a synthetic
clock advanced one simulated day (24h) at a time, matching sim_v2's `day` loop —
all markets generated for the same day share one `now`, exactly like sim_v2 calling
`_get_phase`/computing `effective_edge` once per day before that day's market loop.

No firing-window simulation: the live agent fires immediately once imbalance
enters the momentum band (Option C — see agent/daemon.py's module docstring).
There's no advance signal of when a market will close on this contract (lockTime
is 0 until an operator manually calls closeBetting()), so there's no countdown
window to replay here either — a backtest market either clears the imbalance/phase
bars and fires, or it doesn't.
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict

from rich.console import Console
from rich.table import Table

from ._sim_engine import SimConfigV2
from .config import AgentConfig
from .decision import decide
from .launch_clock import LaunchClock
from .ledger import Ledger
from .perf_log import PerformanceLog
from .pnl import compute_bet_pnl
from .risk_guard import RiskGuard, STATUS_CONFIRMED, STATUS_FAILED

DEFAULT_BACKTEST_PERFORMANCE_LOG_PATH = "logs/backtest_performance.jsonl"
DEFAULT_REVERT_PROB = 0.03  # mirrors PlaceholderChainAdapter's own tx-failure rate


@dataclass
class BacktestResult:
    days: int
    markets_seen: int = 0
    bets_fired: int = 0
    bets_won: int = 0
    bets_lost: int = 0
    bets_reverted: int = 0
    skipped_below_min_imbalance: int = 0
    skipped_above_max_imbalance: int = 0
    skipped_pool_too_small: int = 0
    skipped_risk_guard: Dict[str, int] = field(default_factory=dict)
    final_nav: float = 0.0
    realized_pnl: float = 0.0

    def record_skip_reason(self, reason: str) -> None:
        self.skipped_risk_guard[reason] = self.skipped_risk_guard.get(reason, 0) + 1


def _effective_crowd_vol_mean(cfg: SimConfigV2) -> float:
    baseline_rake = 0.025
    vol_factor = (
        (baseline_rake / cfg.rake_pct) ** cfg.rake_volume_elasticity if cfg.rake_pct > 0 else 2.0
    )
    return cfg.crowd_volume_mean * vol_factor


async def _run(config: AgentConfig, revert_prob: float, performance_log_path: str) -> BacktestResult:
    cfg = config.sim
    rng = random.Random(cfg.seed)
    eff_vol = _effective_crowd_vol_mean(cfg)

    launch_ts = time.time()
    clock = LaunchClock(launch_ts=launch_ts)

    # A fresh, non-existent path each run — RiskGuard.start() initializes new
    # state when the file isn't there yet. Backtests don't test restart
    # persistence (that's covered elsewhere), so there's nothing to load.
    state_path = os.path.join(tempfile.gettempdir(), f"crsh_backtest_risk_{uuid.uuid4().hex}.json")
    risk_guard = RiskGuard(
        state_path=state_path,
        per_bet_cap_usdc=config.per_bet_cap_usdc,
        lifetime_cap_usdc=config.lifetime_cap_usdc,
        circuit_breaker_threshold=config.circuit_breaker_threshold,
    )
    await risk_guard.start()

    ledger = Ledger(starting_capital=cfg.starting_capital)
    perf_log = PerformanceLog(performance_log_path)
    result = BacktestResult(days=cfg.num_days)

    for day in range(cfg.num_days):
        now = launch_ts + day * 86400.0

        cluster_biases = [
            min(max(rng.gauss(cfg.fan_bias_mean, cfg.cluster_bias_std), 0.50), 0.97)
            for _ in range(cfg.num_clusters)
        ]

        for i in range(cfg.markets_per_day):
            market_id = f"bt-d{day:03d}-m{i:03d}"
            result.markets_seen += 1

            cluster_id = rng.randint(0, cfg.num_clusters - 1)
            fan_bias = min(max(rng.gauss(cluster_biases[cluster_id], cfg.market_bias_noise), 0.50), 0.98)
            crowd_vol = max(rng.gauss(eff_vol, cfg.crowd_volume_std), 10.0)

            crowd_yes = crowd_vol * fan_bias
            crowd_no = crowd_vol * (1.0 - fan_bias)
            yes_pool = crowd_yes + cfg.seed_per_side
            no_pool = crowd_no + cfg.seed_per_side

            decision = decide(market_id, yes_pool, no_pool, cfg, clock, now=now)

            if decision.action == "skip":
                if decision.reason == "below min imbalance":
                    result.skipped_below_min_imbalance += 1
                elif decision.reason == "above max imbalance":
                    result.skipped_above_max_imbalance += 1
                elif decision.reason == "pool below minimum":
                    result.skipped_pool_too_small += 1
                continue

            # No firing-window check — fire immediately once imbalance clears
            # the momentum band, matching the live agent's Option C behavior.
            reservation = await risk_guard.check_and_reserve(market_id, decision.side, decision.amount_usdc)
            if not reservation.approved:
                result.record_skip_reason(reservation.reason)
                continue

            result.bets_fired += 1
            perf_log.log_bet_placed(
                market_id, decision.side, decision.amount_usdc, yes_pool, no_pool,
                decision.phase, ledger.realized_pnl,
            )

            if rng.random() < revert_prob:
                await risk_guard.resolve_bet(market_id, STATUS_FAILED)
                result.bets_reverted += 1
                perf_log.log_bet_failed(market_id, decision.side, decision.amount_usdc, "reverted")
                continue

            await risk_guard.resolve_bet(market_id, STATUS_CONFIRMED)
            ledger.open_position(market_id, decision.side, decision.amount_usdc, yes_pool, no_pool)

            outcome_yes = rng.random() < decision.win_prob
            pnl = compute_bet_pnl(yes_pool, no_pool, decision.side, decision.amount_usdc, outcome_yes, cfg.rake_pct)
            ledger.close_position(market_id, pnl)
            perf_log.log_bet_resolved(market_id, decision.side, decision.amount_usdc, outcome_yes, pnl, ledger.realized_pnl)

            if pnl >= 0:
                result.bets_won += 1
            else:
                result.bets_lost += 1

    await risk_guard.stop()
    if os.path.exists(state_path):
        os.remove(state_path)
    result.final_nav = ledger.nav
    result.realized_pnl = ledger.realized_pnl
    return result


def _print_report(result: BacktestResult, config: AgentConfig, performance_log_path: str) -> None:
    console = Console()

    summary = Table(title="Backtest Summary", show_header=False)
    summary.add_column("metric", style="bold")
    summary.add_column("value")
    summary.add_row("Simulated days", str(result.days))
    summary.add_row("Markets seen", str(result.markets_seen))
    summary.add_row("Bets fired", str(result.bets_fired))
    summary.add_row("Bets won / lost", f"{result.bets_won} / {result.bets_lost}")
    summary.add_row("Bets reverted (circuit-breaker input)", str(result.bets_reverted))
    win_rate = (result.bets_won / (result.bets_won + result.bets_lost) * 100) if (result.bets_won + result.bets_lost) else 0.0
    summary.add_row("Win rate", f"{win_rate:.1f}%")
    summary.add_row("Starting capital", f"${config.sim.starting_capital:,.2f}")
    summary.add_row("Final NAV", f"${result.final_nav:,.2f}")
    pnl_style = "green" if result.realized_pnl >= 0 else "red"
    summary.add_row("Realized P&L", f"[{pnl_style}]${result.realized_pnl:,.2f}[/{pnl_style}]")

    skips = Table(title="Skip / Non-Fire Breakdown", show_header=False)
    skips.add_column("reason", style="bold")
    skips.add_column("count")
    skips.add_row("Below MIN_IMBALANCE", str(result.skipped_below_min_imbalance))
    skips.add_row("At/above MAX_IMBALANCE", str(result.skipped_above_max_imbalance))
    skips.add_row("Pool below MIN_POOL_USDC", str(result.skipped_pool_too_small))
    for reason, count in result.skipped_risk_guard.items():
        skips.add_row(f"RiskGuard: {reason}", str(count))

    console.print(summary)
    console.print(skips)
    console.print(f"\nPer-bet performance log written to [bold]{performance_log_path}[/bold]")


def run_backtest(
    config: AgentConfig,
    revert_prob: float = DEFAULT_REVERT_PROB,
    performance_log_path: str = DEFAULT_BACKTEST_PERFORMANCE_LOG_PATH,
) -> BacktestResult:
    result = asyncio.run(_run(config, revert_prob, performance_log_path))
    _print_report(result, config, performance_log_path)
    return result
