"""
CRSH Treasury Simulator — v3 (full model)
==========================================
Seven simulation improvements over v2:

1. Creator cluster bias  — markets group into clusters; each cluster draws a
   shared daily bias shock so fan overconfidence can hit correlated markets.
2. Tunable edge discount — edge_discount ∈ [0,1] controls how wrong the crowd
   is assumed to be (0 = crowd calibrated, 1 = crowd fully backwards).
3. Correlated clusters   — covered by #1; cluster_bias_std controls how much
   clusters diverge from the global mean each day.
4. Sophistication decay  — contrarian edge shrinks day-by-day as sharp bettors
   learn the treasury's pattern.
5. Dynamic exposure cap  — max_contrarian_exposure_pct scales down as treasury
   NAV grows relative to starting capital (avoids oversizing).
6. Rake elasticity       — higher rake_pct reduces crowd volume via a power-law
   elasticity so rake isn't a free lunch.
7. Daily capital limit   — total treasury deployment (seeding + contrarian) is
   capped at daily_capital_limit_pct × current NAV per day.
"""

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple


@dataclass
class SimConfig:
    starting_capital: float = 100_000.0
    num_days: int = 30
    markets_per_day: int = 50

    # ── Base seeding ──────────────────────────────────────────────────────────
    seed_per_side: float = 12.5

    # ── Crowd volume ──────────────────────────────────────────────────────────
    crowd_volume_mean: float = 100.0
    crowd_volume_std: float = 25.0

    # ── Feature 1 & 3: Creator cluster bias (correlated fan overconfidence) ──
    num_clusters: int = 5            # creator clusters per day
    fan_bias_mean: float = 0.55      # global daily bias mean
    cluster_bias_std: float = 0.15   # between-cluster spread (controls correlation)
    market_bias_noise: float = 0.06  # within-cluster residual noise per market

    # ── Feature 2: Tunable contrarian edge ───────────────────────────────────
    # contrarian_win_prob = crowd_implied_minority
    #                      + edge_discount * (fan_bias - crowd_implied_minority)
    # 0 = crowd calibrated (no edge), 0.5 = halfway, 1 = crowd backwards
    edge_discount: float = 0.25
    contrarian_threshold: float = 0.70
    contrarian_activation_prob: float = 0.50
    contrarian_pool_min: float = 50.0
    contrarian_pool_max: float = 150.0
    max_contrarian_exposure_pct: float = 0.30

    # ── Feature 4: Sophistication decay ──────────────────────────────────────
    # effective_edge = edge_discount * max(0, 1 − sophistication_decay × day)
    sophistication_decay: float = 0.02   # fraction of edge lost per day

    # ── Feature 5: Dynamic exposure cap ──────────────────────────────────────
    dynamic_exposure: bool = True  # shrink exposure as NAV grows vs starting

    # ── Feature 6: Rake elasticity ────────────────────────────────────────────
    rake_pct: float = 0.025
    # effective_crowd_vol = crowd_volume_mean × (baseline_rake / rake_pct)^elasticity
    # 0 = inelastic (volume unaffected by rake), 1 = strong decay
    rake_volume_elasticity: float = 0.5

    # ── Feature 7: Daily capital limit ───────────────────────────────────────
    daily_capital_limit_pct: float = 0.20   # max deployable per day as % of NAV

    seed: int = 42


# ── Per-market contrarian bet fraction (convex in imbalance) ─────────────────

def _bet_fraction(fan_bias: float, effective_max_exposure: float,
                  threshold: float) -> float:
    score = (fan_bias - threshold) / (1.0 - threshold)
    return effective_max_exposure * (score ** 2)


# ── One day of markets ────────────────────────────────────────────────────────

def _simulate_day(
    day: int,
    current_nav: float,
    cfg: SimConfig,
    rng: random.Random,
    effective_edge: float,
    effective_crowd_vol_mean: float,
) -> Tuple[float, float, float, List[float]]:
    """
    Returns (base_pnl, contrarian_pnl, rake_income, per_market_total_pnls).
    Rake is always collected from crowd pools regardless of capital limit.
    """
    # Dynamic exposure cap: scale down as NAV grows above starting capital
    if cfg.dynamic_exposure:
        nav_growth = max(1.0, current_nav / cfg.starting_capital)
        eff_max_exposure = cfg.max_contrarian_exposure_pct / nav_growth
    else:
        eff_max_exposure = cfg.max_contrarian_exposure_pct

    # Daily capital limit
    daily_limit = current_nav * cfg.daily_capital_limit_pct
    deployed = 0.0

    # Sample cluster biases once per day (shared shock = correlation)
    cluster_biases = [
        min(max(rng.gauss(cfg.fan_bias_mean, cfg.cluster_bias_std), 0.50), 0.97)
        for _ in range(cfg.num_clusters)
    ]

    d_base = d_con = d_rake = 0.0
    market_pnls: List[float] = []

    for _ in range(cfg.markets_per_day):
        # Assign to cluster → per-market bias
        cluster_id = rng.randint(0, cfg.num_clusters - 1)
        fan_bias = min(max(
            rng.gauss(cluster_biases[cluster_id], cfg.market_bias_noise),
            0.50), 0.98)

        crowd_volume = max(rng.gauss(effective_crowd_vol_mean, cfg.crowd_volume_std), 10.0)
        crowd_yes = crowd_volume * fan_bias
        crowd_no  = crowd_volume * (1.0 - fan_bias)

        # Rake always collected from crowd (not deployment-limited)
        crowd_pool = crowd_yes + crowd_no
        crowd_rake = crowd_pool * cfg.rake_pct

        t_yes = cfg.seed_per_side
        t_no  = cfg.seed_per_side
        base_deploy = t_yes + t_no

        # --- Base seeding (subject to capital limit) -------------------------
        if deployed + base_deploy > daily_limit:
            # Can't seed; still collect rake from crowd
            d_rake += crowd_rake
            market_pnls.append(crowd_rake)
            continue

        deployed += base_deploy
        outcome_yes = rng.random() < 0.5

        pool_yes   = crowd_yes + t_yes
        pool_no    = crowd_no  + t_no
        total_pool = pool_yes  + pool_no
        rake       = total_pool * cfg.rake_pct
        dist       = total_pool - rake

        w_pool = pool_yes if outcome_yes else pool_no
        t_win  = t_yes   if outcome_yes else t_no
        t_payout  = dist * (t_win / w_pool) if w_pool > 0 else 0.0
        base_pnl_m = t_payout - base_deploy
        d_base += base_pnl_m

        # --- Contrarian bet (convex allocation, capital-limited) -------------
        con_pnl_m  = 0.0
        con_rake_m = 0.0

        if fan_bias >= cfg.contrarian_threshold and rng.random() < cfg.contrarian_activation_prob:
            c_size = rng.uniform(cfg.contrarian_pool_min, cfg.contrarian_pool_max)
            c_bet  = c_size * _bet_fraction(fan_bias, eff_max_exposure, cfg.contrarian_threshold)

            if c_bet > 0 and deployed + c_bet <= daily_limit:
                deployed += c_bet

                c_crowd_no  = c_size * (1.0 - fan_bias)
                c_crowd_yes = c_size * fan_bias
                c_pool_no   = c_crowd_no + c_bet
                c_pool_yes  = c_crowd_yes
                c_total     = c_pool_yes + c_pool_no
                con_rake_m  = c_total * cfg.rake_pct
                c_dist      = c_total - con_rake_m

                # Contrarian win probability (tunable edge_discount)
                # 0 → crowd is right (minority wins at crowd-implied prob)
                # 1 → crowd fully backwards (minority wins at majority rate)
                p_crowd = 1.0 - fan_bias
                p_full  = fan_bias
                c_win   = p_crowd + effective_edge * (p_full - p_crowd)

                if rng.random() < c_win:
                    c_payout  = c_dist * (c_bet / c_pool_no)
                    con_pnl_m = c_payout - c_bet
                else:
                    con_pnl_m = -c_bet

        d_con  += con_pnl_m
        d_rake += rake + con_rake_m
        market_pnls.append(base_pnl_m + con_pnl_m + rake + con_rake_m)

    return d_base, d_con, d_rake, market_pnls


# ── Full simulation run ───────────────────────────────────────────────────────

def run_simulation(cfg: SimConfig) -> Dict[str, Any]:
    rng = random.Random(cfg.seed)

    # Rake elasticity: adjust crowd volume for rake level
    baseline_rake = 0.025
    vol_factor = (baseline_rake / cfg.rake_pct) ** cfg.rake_volume_elasticity if cfg.rake_pct > 0 else 2.0
    eff_vol = cfg.crowd_volume_mean * vol_factor

    cap_base = cap_con = cap_rake = cap_floor = cfg.starting_capital
    nav_base  = [cap_base]
    nav_con   = [cap_con]
    nav_rake  = [cap_rake]
    nav_floor = [cap_floor]   # rake-only floor: pure passive income, no betting

    day_results: List[Dict] = []
    all_pnls:    List[float] = []
    d_rakes: List[float] = []
    d_cons:  List[float] = []
    d_bases: List[float] = []

    for day in range(cfg.num_days):
        eff_edge = max(0.0, cfg.edge_discount * (1.0 - cfg.sophistication_decay * day))

        d_base, d_con, d_rake, mkt_pnls = _simulate_day(
            day=day, current_nav=cap_rake, cfg=cfg, rng=rng,
            effective_edge=eff_edge, effective_crowd_vol_mean=eff_vol,
        )

        cap_base  += d_base
        cap_con   += d_base + d_con
        cap_rake  += d_base + d_con + d_rake
        cap_floor += d_rake   # floor: only rake, zero betting variance

        nav_base.append(cap_base)
        nav_con.append(cap_con)
        nav_rake.append(cap_rake)
        nav_floor.append(cap_floor)

        all_pnls.extend(mkt_pnls)
        d_bases.append(d_base)
        d_cons.append(d_con)
        d_rakes.append(d_rake)

        day_results.append({
            "day":            day + 1,
            "effective_edge": round(eff_edge, 4),
            "base_pnl":       d_base,
            "contrarian_pnl": d_con,
            "rake_income":    d_rake,
            "total_pnl":      d_base + d_con + d_rake,
        })

    def _stats(vals: List[float]) -> Dict:
        n    = len(vals)
        mean = sum(vals) / n if n else 0.0
        var  = sum((v - mean) ** 2 for v in vals) / n if n > 1 else 0.0
        return {"mean": mean, "std": math.sqrt(var), "total": sum(vals)}

    def _summary(final: float) -> Dict:
        ret = (final - cfg.starting_capital) / cfg.starting_capital * 100
        return {"final_nav": final, "total_return_pct": ret,
                "total_pnl": final - cfg.starting_capital,
                "profitable": final > cfg.starting_capital}

    return {
        "config":      {k: v for k, v in cfg.__dict__.items()},
        "nav_curves":  {
            "base":            nav_base,
            "plus_contrarian": nav_con,
            "plus_rake":       nav_rake,
            "rake_floor":      nav_floor,
        },
        "days":        day_results,
        "market_pnls": all_pnls,
        "daily_stats": {
            "rake":        _stats(d_rakes),
            "contrarian":  _stats(d_cons),
            "base":        _stats(d_bases),
        },
        "summary": {
            "base":            _summary(cap_base),
            "plus_contrarian": _summary(cap_con),
            "plus_rake":       _summary(cap_rake),
            "rake_floor":      _summary(cap_floor),
            "num_markets":     len(all_pnls),
            "pct_markets_profitable": (
                sum(1 for p in all_pnls if p > 0) / len(all_pnls) * 100
                if all_pnls else 0
            ),
        },
    }
