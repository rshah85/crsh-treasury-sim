"""
CRSH Treasury Simulator — three-layer model
--------------------------------------------
Layer 1 (base):        Treasury seeds $12.5 YES + $12.5 NO on every market.
                       Outcomes are a fair coin flip → real up/down days.
Layer 2 (+contrarian): On 70/30 crowd-imbalanced markets (if activated),
                       treasury also bets 10% of that market's pool on the
                       minority side. Win prob is 30% or 40% (50/50 draw).
Layer 3 (+rake):       The full 2.5% rake from every pool flows to the treasury.

All three layers share the same random draw sequence so they diverge only
based on what each layer adds.
"""

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
    fan_bias_mean: float = 0.55   # crowd imbalance centered near breakeven → realistic up/down days
    fan_bias_std: float = 0.20

    # Contrarian bet settings
    contrarian_threshold: float = 0.70   # crowd split that triggers eligibility
    contrarian_activation_prob: float = 0.50  # % of eligible markets where bet fires
    contrarian_pool_min: float = 50.0
    contrarian_pool_max: float = 150.0
    contrarian_win_prob_low: float = 0.30   # treasury wins this % half the time
    contrarian_win_prob_high: float = 0.40  # and this % the other half

    # Rake
    rake_pct: float = 0.025   # 2.5% of total pool

    seed: int = 42


def _simulate_market(cfg: SimConfig, rng: random.Random):
    """
    Returns (base_pnl, contrarian_pnl, rake_income) for one market.
    base_pnl and contrarian_pnl are computed AFTER rake is removed from the
    distributable pool (rake goes to platform in layers 1 & 2, to treasury
    in layer 3 as rake_income).
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

    winning_pool     = pool_yes if outcome_yes else pool_no
    t_winning_stake  = t_yes   if outcome_yes else t_no
    t_payout = distributable * (t_winning_stake / winning_pool) if winning_pool > 0 else 0.0
    base_pnl = t_payout - (t_yes + t_no)

    # --- contrarian bet ---
    contrarian_pnl  = 0.0
    contrarian_rake = 0.0

    is_contrarian = fan_bias >= cfg.contrarian_threshold
    activated     = is_contrarian and (rng.random() < cfg.contrarian_activation_prob)

    if activated:
        c_size     = rng.uniform(cfg.contrarian_pool_min, cfg.contrarian_pool_max)
        c_crowd_yes = c_size * fan_bias          # majority (crowded) side
        c_crowd_no  = c_size * (1.0 - fan_bias)  # minority side
        c_bet       = c_size * 0.10              # 10% of pool on minority (NO)

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

    # Three independent capital accounts
    cap_base = cfg.starting_capital
    cap_con  = cfg.starting_capital
    cap_rake = cfg.starting_capital

    nav_base = [cap_base]
    nav_con  = [cap_con]
    nav_rake = [cap_rake]

    day_results: List[Dict] = []
    all_market_pnls: List[float] = []

    for day in range(cfg.num_days):
        d_base = d_con = d_rake = 0.0

        for _ in range(cfg.markets_per_day):
            bp, cp, rk = _simulate_market(cfg, rng)
            d_base += bp
            d_con  += cp
            d_rake += rk
            all_market_pnls.append(bp + cp + rk)

        cap_base += d_base
        cap_con  += d_base + d_con
        cap_rake += d_base + d_con + d_rake

        nav_base.append(cap_base)
        nav_con.append(cap_con)
        nav_rake.append(cap_rake)

        day_results.append({
            "day": day + 1,
            "base_pnl": d_base,
            "contrarian_pnl": d_con,
            "rake_income": d_rake,
            "total_pnl": d_base + d_con + d_rake,
        })

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
        },
        "days": day_results,
        "market_pnls": all_market_pnls,
        "summary": {
            "base":             layer_summary(cap_base),
            "plus_contrarian":  layer_summary(cap_con),
            "plus_rake":        layer_summary(cap_rake),
            "num_markets":      cfg.num_days * cfg.markets_per_day,
            "pct_markets_profitable": (
                sum(1 for p in all_market_pnls if p > 0) / len(all_market_pnls) * 100
                if all_market_pnls else 0
            ),
        },
    }
