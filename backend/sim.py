"""
CRSH Treasury Simulator — three-layer model
--------------------------------------------
Layer 1 (base):        Treasury seeds $12.5 YES + $12.5 NO on every market.
                       Outcomes are a fair coin flip → real up/down days.
Layer 2 (+contrarian): On 70/30+ imbalanced markets (if activated), treasury
                       bets the minority side. Allocation is convex: near-
                       threshold markets get almost nothing; extreme (90/10+)
                       markets get up to max_contrarian_exposure_pct of the pool.
Layer 3 (+rake):       The full 2.5% rake from every pool flows to the treasury.

Rake income is the stable floor. Contrarian is the high-variance overlay.
"""

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class SimConfig:
    starting_capital: float = 100_000.0
    num_days: int = 30
    markets_per_day: int = 50

    # Base seeding
    seed_per_side: float = 12.5
    crowd_volume_mean: float = 100.0
    crowd_volume_std: float = 25.0

    # Crowd imbalance distribution
    fan_bias_mean: float = 0.55
    fan_bias_std: float = 0.20

    # Contrarian bet settings
    contrarian_threshold: float = 0.70        # minimum crowd split to be eligible
    contrarian_activation_prob: float = 0.50  # % of eligible markets where bet fires
    contrarian_pool_min: float = 50.0
    contrarian_pool_max: float = 150.0
    contrarian_win_prob_low: float = 0.30
    contrarian_win_prob_high: float = 0.40
    # Convex allocation: bet fraction = max_exposure * imbalance_score^2
    # imbalance_score = (fan_bias - threshold) / (1 - threshold)
    # → near-threshold markets get ~0, extreme (90/10) markets approach max
    max_contrarian_exposure_pct: float = 0.30  # max fraction of pool treasury bets

    # Rake
    rake_pct: float = 0.025

    seed: int = 42


def _contrarian_bet_fraction(fan_bias: float, cfg: SimConfig) -> float:
    """Convex allocation: quadratic in imbalance score so extreme splits dominate."""
    imbalance_score = (fan_bias - cfg.contrarian_threshold) / (1.0 - cfg.contrarian_threshold)
    return cfg.max_contrarian_exposure_pct * (imbalance_score ** 2)


def _simulate_market(cfg: SimConfig, rng: random.Random):
    """
    Returns (base_pnl, contrarian_pnl, rake_income) for one market.
    Rake is always computed but only flows to treasury in layer 3.
    """
    # --- crowd ---
    crowd_volume = max(rng.gauss(cfg.crowd_volume_mean, cfg.crowd_volume_std), 10.0)
    fan_bias = min(max(rng.gauss(cfg.fan_bias_mean, cfg.fan_bias_std), 0.50), 0.98)
    crowd_yes = crowd_volume * fan_bias
    crowd_no  = crowd_volume * (1.0 - fan_bias)

    # --- base treasury stakes ---
    t_yes = cfg.seed_per_side
    t_no  = cfg.seed_per_side

    # --- fair-coin outcome ---
    outcome_yes = rng.random() < 0.5

    pool_yes   = crowd_yes + t_yes
    pool_no    = crowd_no  + t_no
    total_pool = pool_yes  + pool_no

    rake          = total_pool * cfg.rake_pct
    distributable = total_pool - rake

    winning_pool    = pool_yes if outcome_yes else pool_no
    t_winning_stake = t_yes   if outcome_yes else t_no
    t_payout  = distributable * (t_winning_stake / winning_pool) if winning_pool > 0 else 0.0
    base_pnl  = t_payout - (t_yes + t_no)

    # --- contrarian bet (convex allocation by imbalance severity) ---
    contrarian_pnl  = 0.0
    contrarian_rake = 0.0

    is_contrarian = fan_bias >= cfg.contrarian_threshold
    activated     = is_contrarian and (rng.random() < cfg.contrarian_activation_prob)

    if activated:
        c_size      = rng.uniform(cfg.contrarian_pool_min, cfg.contrarian_pool_max)
        c_crowd_yes = c_size * fan_bias
        c_crowd_no  = c_size * (1.0 - fan_bias)

        # Convex allocation: near-threshold ≈ 0%, extreme split ≈ max_exposure
        bet_fraction = _contrarian_bet_fraction(fan_bias, cfg)
        c_bet = c_size * bet_fraction

        c_pool_yes = c_crowd_yes
        c_pool_no  = c_crowd_no + c_bet
        c_total    = c_pool_yes + c_pool_no
        c_rake     = c_total * cfg.rake_pct
        c_dist     = c_total - c_rake
        contrarian_rake = c_rake

        # Win probability for minority: randomly 30% or 40%
        c_win_prob = (cfg.contrarian_win_prob_high
                      if rng.random() < 0.5
                      else cfg.contrarian_win_prob_low)
        no_wins = rng.random() < c_win_prob

        if no_wins:
            c_payout       = c_dist * (c_bet / c_pool_no)
            contrarian_pnl = c_payout - c_bet
        else:
            contrarian_pnl = -c_bet

    return base_pnl, contrarian_pnl, rake + contrarian_rake


def run_simulation(cfg: SimConfig) -> Dict[str, Any]:
    rng = random.Random(cfg.seed)

    cap_base      = cfg.starting_capital
    cap_con       = cfg.starting_capital
    cap_rake      = cfg.starting_capital
    cap_rake_only = cfg.starting_capital   # pure rake floor: no betting at all

    nav_base      = [cap_base]
    nav_con       = [cap_con]
    nav_rake      = [cap_rake]
    nav_rake_only = [cap_rake_only]

    day_results: List[Dict] = []
    all_market_pnls: List[float] = []

    daily_rake_income: List[float] = []
    daily_contrarian_pnl: List[float] = []
    daily_base_pnl: List[float] = []

    for day in range(cfg.num_days):
        d_base = d_con = d_rake = 0.0

        for _ in range(cfg.markets_per_day):
            bp, cp, rk = _simulate_market(cfg, rng)
            d_base += bp
            d_con  += cp
            d_rake += rk
            all_market_pnls.append(bp + cp + rk)

        cap_base      += d_base
        cap_con       += d_base + d_con
        cap_rake      += d_base + d_con + d_rake
        cap_rake_only += d_rake   # rake floor: only rake, no betting exposure

        nav_base.append(cap_base)
        nav_con.append(cap_con)
        nav_rake.append(cap_rake)
        nav_rake_only.append(cap_rake_only)

        daily_base_pnl.append(d_base)
        daily_contrarian_pnl.append(d_con)
        daily_rake_income.append(d_rake)

        day_results.append({
            "day": day + 1,
            "base_pnl": d_base,
            "contrarian_pnl": d_con,
            "rake_income": d_rake,
            "total_pnl": d_base + d_con + d_rake,
        })

    def _stats(vals: List[float]) -> Dict:
        n = len(vals)
        mean = sum(vals) / n if n else 0.0
        variance = sum((v - mean) ** 2 for v in vals) / n if n > 1 else 0.0
        std = math.sqrt(variance)
        return {"mean": mean, "std": std, "total": sum(vals)}

    def layer_summary(final: float) -> Dict:
        ret = (final - cfg.starting_capital) / cfg.starting_capital * 100
        return {
            "final_nav": final,
            "total_return_pct": ret,
            "total_pnl": final - cfg.starting_capital,
            "profitable": final > cfg.starting_capital,
        }

    return {
        "config": cfg.__dict__,
        "nav_curves": {
            "base":             nav_base,
            "plus_contrarian":  nav_con,
            "plus_rake":        nav_rake,
            "rake_floor":       nav_rake_only,   # steady upward slope, no betting variance
        },
        "days": day_results,
        "market_pnls": all_market_pnls,
        "daily_stats": {
            "rake":        _stats(daily_rake_income),
            "contrarian":  _stats(daily_contrarian_pnl),
            "base":        _stats(daily_base_pnl),
        },
        "summary": {
            "base":             layer_summary(cap_base),
            "plus_contrarian":  layer_summary(cap_con),
            "plus_rake":        layer_summary(cap_rake),
            "rake_floor":       layer_summary(cap_rake_only),
            "num_markets":      cfg.num_days * cfg.markets_per_day,
            "pct_markets_profitable": (
                sum(1 for p in all_market_pnls if p > 0) / len(all_market_pnls) * 100
                if all_market_pnls else 0
            ),
        },
    }
