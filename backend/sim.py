"""
CRSH Treasury Simulator
------------------------
Simulates a community-owned treasury that seeds every pari-mutuel
prediction market with a symmetric base allocation plus a contrarian
overlay that leans into the minority side as crowd imbalance grows.

Core mechanics per market:
  1. Crowd betting is generated with a fan-bias skew toward a "favorite" side.
  2. Treasury allocates: base symmetric seed on both sides + contrarian
     overlay proportional to imbalance, scaled by `contrarian_aggressiveness`.
  3. Pool resolves (favorite wins with probability `favorite_win_prob`,
     independent of how much was bet on it -- this is what creates the
     behavioral inefficiency the treasury harvests).
  4. Rake is taken off the top of the total pool before payout.
  5. Treasury P&L = its share of the winning pool (proportional to its
     stake in the winning side) - its stake, plus its pro-rata share of
     the rake (the rake share modeled as a fixed platform-to-treasury cut).
"""

import random
from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class SimConfig:
    starting_capital: float = 100_000.0
    markets_per_epoch: int = 50
    num_epochs: int = 20
    rake_pct: float = 0.025          # total platform rake, e.g. 2.5%
    treasury_rake_share: float = 0.5  # fraction of rake that flows to treasury
    seed_per_side: float = 12.5       # symmetric base seed per side
    contrarian_aggressiveness: float = 1.0  # scalar on the contrarian overlay
    fan_bias_mean: float = 0.75       # avg fraction of crowd money on favorite
    fan_bias_std: float = 0.1         # variance of fan bias across markets
    favorite_win_prob: float = 0.55   # true probability favorite wins
    crowd_volume_mean: float = 1000.0 # average total crowd stake per market
    crowd_volume_std: float = 300.0
    max_exposure_pct: float = 0.05    # max % of treasury capital in one market (per side)
    seed: int = 42


@dataclass
class MarketResult:
    epoch: int
    market_idx: int
    crowd_yes: float
    crowd_no: float
    treasury_yes: float
    treasury_no: float
    favorite_is_yes: bool
    outcome_yes: bool
    total_pool: float
    rake_amount: float
    treasury_rake_income: float
    treasury_pnl_from_pool: float
    treasury_pnl_total: float


@dataclass
class EpochResult:
    epoch: int
    starting_nav: float
    ending_nav: float
    fee_income: float
    contrarian_edge: float
    total_pnl: float
    markets: List[MarketResult] = field(default_factory=list)


def _clip_treasury_stake(stake: float, capital: float, max_exposure_pct: float) -> float:
    cap = capital * max_exposure_pct
    return min(stake, max(cap, 0.0))


def simulate_market(epoch: int, idx: int, capital: float, cfg: SimConfig, rng: random.Random) -> MarketResult:
    # 1. crowd behavior
    total_volume = max(rng.gauss(cfg.crowd_volume_mean, cfg.crowd_volume_std), 50.0)
    fan_bias = min(max(rng.gauss(cfg.fan_bias_mean, cfg.fan_bias_std), 0.5), 0.99)
    favorite_is_yes = rng.random() < 0.5

    favorite_volume = total_volume * fan_bias
    underdog_volume = total_volume * (1 - fan_bias)
    crowd_yes = favorite_volume if favorite_is_yes else underdog_volume
    crowd_no = underdog_volume if favorite_is_yes else favorite_volume

    # 2. treasury allocation: symmetric seed + contrarian overlay
    base_yes = cfg.seed_per_side
    base_no = cfg.seed_per_side

    crowd_total = crowd_yes + crowd_no
    imbalance = (crowd_yes - crowd_no) / crowd_total if crowd_total > 0 else 0.0
    # imbalance > 0 means YES is the crowded side -> overlay leans NO, and vice versa
    overlay_pool = cfg.seed_per_side * 2 * cfg.contrarian_aggressiveness * abs(imbalance)
    if imbalance > 0:
        overlay_yes, overlay_no = 0.0, overlay_pool
    else:
        overlay_yes, overlay_no = overlay_pool, 0.0

    treasury_yes = _clip_treasury_stake(base_yes + overlay_yes, capital, cfg.max_exposure_pct)
    treasury_no = _clip_treasury_stake(base_no + overlay_no, capital, cfg.max_exposure_pct)

    # 3. resolve outcome (independent of betting volume -- the inefficiency)
    favorite_wins = rng.random() < cfg.favorite_win_prob
    outcome_yes = favorite_wins if favorite_is_yes else (not favorite_wins)

    pool_yes = crowd_yes + treasury_yes
    pool_no = crowd_no + treasury_no
    total_pool = pool_yes + pool_no

    # 4. rake off the top
    rake_amount = total_pool * cfg.rake_pct
    distributable = total_pool - rake_amount
    treasury_rake_income = rake_amount * cfg.treasury_rake_share

    # 5. payout: winning side splits `distributable` proportional to stake
    winning_pool = pool_yes if outcome_yes else pool_no
    treasury_winning_stake = treasury_yes if outcome_yes else treasury_no
    treasury_stake_total = treasury_yes + treasury_no

    if winning_pool > 0:
        treasury_payout = distributable * (treasury_winning_stake / winning_pool)
    else:
        treasury_payout = 0.0

    treasury_pnl_from_pool = treasury_payout - treasury_stake_total
    treasury_pnl_total = treasury_pnl_from_pool + treasury_rake_income

    return MarketResult(
        epoch=epoch,
        market_idx=idx,
        crowd_yes=crowd_yes,
        crowd_no=crowd_no,
        treasury_yes=treasury_yes,
        treasury_no=treasury_no,
        favorite_is_yes=favorite_is_yes,
        outcome_yes=outcome_yes,
        total_pool=total_pool,
        rake_amount=rake_amount,
        treasury_rake_income=treasury_rake_income,
        treasury_pnl_from_pool=treasury_pnl_from_pool,
        treasury_pnl_total=treasury_pnl_total,
    )


def run_simulation(cfg: SimConfig) -> Dict[str, Any]:
    rng = random.Random(cfg.seed)
    capital = cfg.starting_capital
    nav_curve = [capital]
    epoch_results: List[EpochResult] = []
    all_market_pnls: List[float] = []

    for epoch in range(cfg.num_epochs):
        starting_nav = capital
        fee_income = 0.0
        contrarian_edge = 0.0
        markets: List[MarketResult] = []

        for i in range(cfg.markets_per_epoch):
            m = simulate_market(epoch, i, capital, cfg, rng)
            markets.append(m)
            fee_income += m.treasury_rake_income
            contrarian_edge += m.treasury_pnl_from_pool
            capital += m.treasury_pnl_total
            all_market_pnls.append(m.treasury_pnl_total)

        epoch_results.append(EpochResult(
            epoch=epoch,
            starting_nav=starting_nav,
            ending_nav=capital,
            fee_income=fee_income,
            contrarian_edge=contrarian_edge,
            total_pnl=fee_income + contrarian_edge,
            markets=markets,
        ))
        nav_curve.append(capital)

    return {
        "config": cfg.__dict__,
        "nav_curve": nav_curve,
        "epochs": [
            {
                "epoch": e.epoch,
                "starting_nav": e.starting_nav,
                "ending_nav": e.ending_nav,
                "fee_income": e.fee_income,
                "contrarian_edge": e.contrarian_edge,
                "total_pnl": e.total_pnl,
            }
            for e in epoch_results
        ],
        "market_pnls": all_market_pnls,
        "summary": {
            "final_nav": capital,
            "total_return_pct": (capital - cfg.starting_capital) / cfg.starting_capital * 100,
            "total_fee_income": sum(e.fee_income for e in epoch_results),
            "total_contrarian_edge": sum(e.contrarian_edge for e in epoch_results),
            "num_markets_simulated": len(all_market_pnls),
            "avg_market_pnl": sum(all_market_pnls) / len(all_market_pnls) if all_market_pnls else 0,
            "pct_markets_profitable": (
                sum(1 for p in all_market_pnls if p > 0) / len(all_market_pnls) * 100
                if all_market_pnls else 0
            ),
        },
    }
