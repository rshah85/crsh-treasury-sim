"""
Decision engine for the live CRSH momentum agent.

Reuses sim_v2's validated Kelly sizing (`_kelly_bet_size`) directly — this module's
job is only to translate live pool state into the same inputs the backtest already
feeds that math, via LaunchClock (see launch_clock.py for why a wall-clock adapter
is needed at all).

Strategy (momentum, not contrarian): fire only when the majority side holds
[MIN_IMBALANCE, MAX_IMBALANCE) of the pool AND the pool is at least MIN_POOL_USDC,
then bet the MAJORITY (favorite) side. This replaced the original contrarian
design (bet the underdog once imbalance passed a threshold) after backtesting
3,432 resolved on-chain markets showed the 70-79% imbalance band is the only
imbalance bucket where crowd-favorite betting was net +EV (n=62, +$32.01 total);
every other band, and the >=70% band as a whole, was net -EV once rake is
accounted for. MOMENTUM_WIN_PROB is that band's empirical favorite win rate.

MAX_IMBALANCE is an exclusive upper bound ("above 80% don't bet") — extreme
imbalance markets get skipped, not bet, since the backtest showed favorite EV
does not hold up there (payout odds get too thin to clear the rake).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .launch_clock import LaunchClock
from ._sim_engine import SimConfigV2, _kelly_bet_size

MIN_IMBALANCE = 0.70
MAX_IMBALANCE = 0.80  # exclusive — majority_share >= this is skipped, not bet
MIN_POOL_USDC = 50.0  # lowered from 200 — historical median pool is $84.50

# Empirical favorite win rate in the 70-79% imbalance bucket, from backtesting
# 3,432 resolved on-chain markets (n=62 qualifying bets in that bucket). Used
# directly as the Kelly win_prob input rather than a formula derived from
# effective_edge, since this is a measured base rate for the exact band we fire in.
MOMENTUM_WIN_PROB = 0.871

# Hard ceiling on any single bet, applied after Kelly sizing. Kelly still
# determines relative sizing below this cap; it just can't push a bet above it.
# In place while validating the new momentum strategy on a $310 bankroll, to
# bound the damage of any one bad bet.
MAX_BET_USDC = 22.5


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


def _phase_kelly_scalar(cfg: SimConfigV2, phase: int) -> float:
    if phase == 1:
        return cfg.phase1_kelly_scalar
    elif phase == 2:
        return cfg.phase2_kelly_scalar
    return cfg.phase3_kelly_scalar


def decide(
    market_id: str,
    yes_pool: float,
    no_pool: float,
    cfg: SimConfigV2,
    clock: LaunchClock,
    now: Optional[float] = None,
) -> Decision:
    """Compute the theoretical momentum bet for one market's current pool state.

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

    if majority_share < MIN_IMBALANCE:
        return Decision(
            market_id, "skip", None, 0.0, "below min imbalance", phase, effective_edge, majority_share
        )

    if majority_share >= MAX_IMBALANCE:
        return Decision(
            market_id, "skip", None, 0.0, "above max imbalance", phase, effective_edge, majority_share
        )

    if total_pool < MIN_POOL_USDC:
        return Decision(
            market_id, "skip", None, 0.0, "pool below minimum", phase, effective_edge, majority_share
        )

    # Bet the favorite: the side with the LARGER pool share.
    side = "YES" if yes_share >= no_share else "NO"

    win_prob = MOMENTUM_WIN_PROB
    kelly_scalar = _phase_kelly_scalar(cfg, phase)

    amount = _kelly_bet_size(
        c_size=total_pool,
        fan_bias=1.0 - majority_share,
        win_prob=win_prob,
        kelly_scalar=kelly_scalar,
        max_kelly_fraction=cfg.max_kelly_fraction,
        rake_pct=cfg.rake_pct,
    )
    amount = min(amount, MAX_BET_USDC)

    if amount <= 0:
        return Decision(
            market_id, "skip", None, 0.0, "zero kelly size", phase, effective_edge, majority_share
        )

    return Decision(
        market_id, "bet", side, amount, "momentum + kelly sized", phase, effective_edge, majority_share,
        win_prob,
    )
