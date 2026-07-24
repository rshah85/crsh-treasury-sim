"""
Decision engine for the live CRSH contrarian agent.

Reuses sim_v2's validated Kelly sizing (`_kelly_bet_size`) and 3-phase aggression
schedule directly — this module's job is only to translate live pool state into the
same inputs the backtest already feeds that math, via LaunchClock (see
launch_clock.py for why a wall-clock adapter is needed at all).

Two distinct thresholds are in play, deliberately not collapsed into one:
  - IMBALANCE_THRESHOLD (0.70): the "is this market even worth looking at" trigger
    from the design doc ("crowd imbalance past 70/30"). This is what the daemon uses
    to decide a market is in-window for firing consideration at all.
  - phase{1,2,3}_threshold (from SimConfigV2, 0.65/0.70/0.75): sim_v2's existing
    per-phase bar for whether a contrarian bet actually fires once a market clears
    the imbalance trigger, reused unchanged. Phase 3 (most selective) can require
    MORE imbalance than the 0.70 trigger; phase 1 can require less — this is
    intentional and matches the backtest's existing aggression schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .launch_clock import LaunchClock
from ._sim_engine import SimConfigV2, _kelly_bet_size

IMBALANCE_THRESHOLD = 0.60


@dataclass(frozen=True)
class Decision:
    market_id: str
    action: str  # "bet" | "skip"
    side: Optional[str]  # "YES" | "NO" — the side the agent would bet, if action=="bet"
    amount_usdc: float
    reason: str
    phase: int
    effective_edge: float
    majority_share: float
    # Only meaningful when action == "bet" — the win probability decision.py used
    # to size this bet. Exposed so backtest.py can draw a realistic bet-outcome
    # sample without re-deriving win_prob's formula outside this module.
    win_prob: float = 0.0


def _phase_params(cfg: SimConfigV2, phase: int) -> tuple[float, float]:
    if phase == 1:
        return cfg.phase1_threshold, cfg.phase1_kelly_scalar
    elif phase == 2:
        return cfg.phase2_threshold, cfg.phase2_kelly_scalar
    return cfg.phase3_threshold, cfg.phase3_kelly_scalar


def decide(
    market_id: str,
    yes_pool: float,
    no_pool: float,
    cfg: SimConfigV2,
    clock: LaunchClock,
    now: Optional[float] = None,
) -> Decision:
    """Compute the theoretical contrarian bet for one market's current pool state.

    This is pure sizing logic — it does NOT know about the firing-time window,
    exposure caps, already-bet-this-market state, or the circuit breaker. Those are
    RiskGuard's and the daemon's job (agent/risk_guard.py, agent/daemon.py); this
    function is called only after the daemon has already decided the market is
    worth evaluating this tick.
    """
    total_pool = yes_pool + no_pool
    phase = clock.phase(now)
    effective_edge = clock.effective_edge(cfg.edge_discount, cfg.sophistication_decay, now)

    if total_pool <= 0:
        return Decision(market_id, "skip", None, 0.0, "empty pool", phase, effective_edge, 0.0)

    yes_share = yes_pool / total_pool
    no_share = 1.0 - yes_share
    majority_share = max(yes_share, no_share)

    if majority_share <= IMBALANCE_THRESHOLD:
        return Decision(
            market_id, "skip", None, 0.0, "no imbalance", phase, effective_edge, majority_share
        )

    threshold, kelly_scalar = _phase_params(cfg, phase)
    if majority_share < threshold:
        return Decision(
            market_id,
            "skip",
            None,
            0.0,
            f"below phase {phase} threshold ({threshold})",
            phase,
            effective_edge,
            majority_share,
        )

    # Bet the underdog: the side with the SMALLER pool share.
    side = "NO" if yes_share >= no_share else "YES"

    p_crowd = 1.0 - majority_share
    p_full = majority_share
    win_prob = p_crowd + effective_edge * (p_full - p_crowd)

    amount = _kelly_bet_size(
        c_size=total_pool,
        fan_bias=majority_share,
        win_prob=win_prob,
        kelly_scalar=kelly_scalar,
        max_kelly_fraction=cfg.max_kelly_fraction,
        rake_pct=cfg.rake_pct,
    )

    if amount <= 0:
        return Decision(
            market_id, "skip", None, 0.0, "zero kelly size", phase, effective_edge, majority_share
        )

    return Decision(
        market_id, "bet", side, amount, "imbalance + kelly sized", phase, effective_edge, majority_share,
        win_prob,
    )
