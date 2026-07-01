"""
CRSH Treasury Simulator — v2 Engine
=====================================
Five structural improvements over v1:

1. Kelly-sized contrarian bets
   Replaces fixed convex fraction with Kelly criterion:
   f* = (p·b − q) / b, where b = (YES_pool / NO_pool) × (1 − rake).
   Capped at max_kelly_fraction; scaled by per-phase kelly_scalar.

2. Daily market priority scoring
   All markets for a day are scored by bias strength before capital is
   allocated. Budget deploys to highest-priority markets first.

3. 3-phase time-decay aggression schedule
   Phases split the run into thirds. Phase 1 is most aggressive
   (low threshold, full Kelly). Phase 3 is most selective (high threshold,
   half Kelly). Simulates learner curve → risk reduction over time.

4. Rolling daily capital budget with carryover
   Unused daily budget carries over to the next day (up to carryover_cap).
   Models realistic "save dry-powder, redeploy on heavy-action days" logic.

5. Volume-weighted rake harvesting mode
   Tracks cumulative crowd volume. When it crosses
   harvest_trigger_pct × volume_cap, the engine stops contrarian bets and
   runs rake-only mode — protecting profits near the platform volume limit.
"""

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple


@dataclass
class SimConfigV2:
    # ── Shared base params (mirrors v1 SimConfig) ─────────────────────────────
    starting_capital: float = 100_000.0
    num_days: int = 30
    markets_per_day: int = 50
    seed_per_side: float = 12.5
    crowd_volume_mean: float = 100.0
    crowd_volume_std: float = 25.0
    num_clusters: int = 5
    fan_bias_mean: float = 0.55
    cluster_bias_std: float = 0.15
    market_bias_noise: float = 0.06
    edge_discount: float = 0.25
    contrarian_activation_prob: float = 0.50
    contrarian_pool_min: float = 50.0
    contrarian_pool_max: float = 150.0
    rake_pct: float = 0.025
    rake_volume_elasticity: float = 0.5
    daily_capital_limit_pct: float = 0.20
    sophistication_decay: float = 0.02
    seed: int = 42

    # ── V2: 3-phase aggression schedule ───────────────────────────────────────
    phase1_threshold: float = 0.65
    phase2_threshold: float = 0.70
    phase3_threshold: float = 0.75
    phase1_kelly_scalar: float = 1.00   # full Kelly in early phase
    phase2_kelly_scalar: float = 0.70
    phase3_kelly_scalar: float = 0.50   # half-Kelly in late phase

    # ── V2: Kelly sizing ───────────────────────────────────────────────────────
    max_kelly_fraction: float = 0.25    # hard cap on f* regardless of formula

    # ── V2: Carryover ─────────────────────────────────────────────────────────
    carryover_cap_pct: float = 0.50     # max carryover as fraction of daily limit

    # ── V2: Rake harvest mode ─────────────────────────────────────────────────
    volume_cap: float = 5_000_000.0     # total platform volume cap ($)
    harvest_trigger_pct: float = 0.80   # trigger harvest at 80% of cap


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_phase(day: int, num_days: int) -> int:
    frac = day / max(num_days - 1, 1)
    if frac < 1 / 3:
        return 1
    elif frac < 2 / 3:
        return 2
    return 3


def _kelly_bet_size(
    c_size: float,
    fan_bias: float,
    win_prob: float,
    kelly_scalar: float,
    max_kelly_fraction: float,
    rake_pct: float,
) -> float:
    """
    Compute Kelly-optimal bet on the NO side of a pari-mutuel market.
    Approximates net odds as (YES_crowd / NO_crowd) × (1 − rake).
    """
    c_crowd_yes = c_size * fan_bias
    c_crowd_no  = c_size * (1.0 - fan_bias)
    if c_crowd_no <= 0:
        return 0.0

    b = (c_crowd_yes / c_crowd_no) * (1.0 - rake_pct)
    p = win_prob
    q = 1.0 - p
    kelly_f = max(0.0, (p * b - q) / b) if b > 0 else 0.0
    kelly_f = min(kelly_f, max_kelly_fraction) * kelly_scalar
    return c_size * kelly_f


def _market_priority_score(fan_bias: float, cluster_bias: float) -> float:
    """Score a market for capital priority. Higher = deploy here first."""
    return fan_bias + 0.1 * (fan_bias - cluster_bias)  # bias + cluster alignment bonus


# ── One day of markets (v2 engine) ────────────────────────────────────────────

def _simulate_day_v2(
    day: int,
    current_nav: float,
    cfg: SimConfigV2,
    rng: random.Random,
    effective_edge: float,
    effective_crowd_vol_mean: float,
    carryover: float,
    harvest_mode: bool,
    cumulative_volume: float,
) -> Tuple[float, float, float, List[float], float, float, float, float, bool, float]:
    """
    Returns:
        base_pnl, contrarian_pnl, rake_income,
        per_market_pnls, new_carryover, capital_deployed, capital_budget,
        day_crowd_volume, harvest_triggered_this_day, new_cumulative_volume
    """
    phase = _get_phase(day, cfg.num_days)
    if phase == 1:
        threshold     = cfg.phase1_threshold
        kelly_scalar  = cfg.phase1_kelly_scalar
    elif phase == 2:
        threshold     = cfg.phase2_threshold
        kelly_scalar  = cfg.phase2_kelly_scalar
    else:
        threshold     = cfg.phase3_threshold
        kelly_scalar  = cfg.phase3_kelly_scalar

    # Capital budget = daily limit + carryover
    daily_limit    = current_nav * cfg.daily_capital_limit_pct
    capital_budget = daily_limit + carryover
    deployed       = 0.0

    # Cluster biases
    cluster_biases = [
        min(max(rng.gauss(cfg.fan_bias_mean, cfg.cluster_bias_std), 0.50), 0.97)
        for _ in range(cfg.num_clusters)
    ]

    # ── Generate all markets for the day ─────────────────────────────────────
    markets = []
    day_crowd_volume = 0.0
    for _ in range(cfg.markets_per_day):
        cluster_id  = rng.randint(0, cfg.num_clusters - 1)
        fan_bias    = min(max(rng.gauss(cluster_biases[cluster_id], cfg.market_bias_noise), 0.50), 0.98)
        crowd_vol   = max(rng.gauss(effective_crowd_vol_mean, cfg.crowd_volume_std), 10.0)
        c_size      = rng.uniform(cfg.contrarian_pool_min, cfg.contrarian_pool_max)
        markets.append({
            "fan_bias":    fan_bias,
            "cluster_bias": cluster_biases[cluster_id],
            "crowd_vol":   crowd_vol,
            "c_size":      c_size,
            "cluster_id":  cluster_id,
        })
        day_crowd_volume += crowd_vol * (1 + fan_bias)  # yes+no volume

    # ── Sort by priority score (highest bias → deploy first) ─────────────────
    markets.sort(key=lambda m: _market_priority_score(m["fan_bias"], m["cluster_bias"]), reverse=True)

    d_base = d_con = d_rake = 0.0
    market_pnls: List[float] = []
    harvest_triggered_this_day = False
    new_cumulative_volume = cumulative_volume + day_crowd_volume

    harvest_threshold = cfg.volume_cap * cfg.harvest_trigger_pct

    for m in markets:
        fan_bias   = m["fan_bias"]
        crowd_vol  = m["crowd_vol"]
        c_size     = m["c_size"]

        # Check if we've crossed the harvest threshold mid-day
        if not harvest_mode and new_cumulative_volume >= harvest_threshold:
            harvest_mode = True
            harvest_triggered_this_day = True

        crowd_yes = crowd_vol * fan_bias
        crowd_no  = crowd_vol * (1.0 - fan_bias)
        crowd_pool = crowd_yes + crowd_no
        crowd_rake = crowd_pool * cfg.rake_pct

        t_yes = cfg.seed_per_side
        t_no  = cfg.seed_per_side
        base_deploy = t_yes + t_no

        # Base seeding (capital-limited)
        if deployed + base_deploy > capital_budget:
            d_rake      += crowd_rake
            market_pnls.append(crowd_rake)
            continue

        deployed    += base_deploy
        outcome_yes  = rng.random() < 0.5

        pool_yes   = crowd_yes + t_yes
        pool_no    = crowd_no  + t_no
        total_pool = pool_yes  + pool_no
        rake       = total_pool * cfg.rake_pct
        dist       = total_pool - rake

        w_pool    = pool_yes if outcome_yes else pool_no
        t_win     = t_yes   if outcome_yes else t_no
        t_payout  = dist * (t_win / w_pool) if w_pool > 0 else 0.0
        base_pnl_m = t_payout - base_deploy
        d_base    += base_pnl_m

        # Contrarian bet (Kelly-sized, priority-ordered, skip in harvest mode)
        con_pnl_m  = 0.0
        con_rake_m = 0.0

        if (
            not harvest_mode
            and fan_bias >= threshold
            and rng.random() < cfg.contrarian_activation_prob
        ):
            # Kelly bet sizing
            p_crowd = 1.0 - fan_bias
            p_full  = fan_bias
            c_win   = p_crowd + effective_edge * (p_full - p_crowd)

            c_bet = _kelly_bet_size(
                c_size, fan_bias, c_win,
                kelly_scalar, cfg.max_kelly_fraction, cfg.rake_pct,
            )

            if c_bet > 0 and deployed + c_bet <= capital_budget:
                deployed += c_bet

                c_crowd_no  = c_size * (1.0 - fan_bias)
                c_crowd_yes = c_size * fan_bias
                c_pool_no   = c_crowd_no + c_bet
                c_pool_yes  = c_crowd_yes
                c_total     = c_pool_yes + c_pool_no
                con_rake_m  = c_total * cfg.rake_pct
                c_dist      = c_total - con_rake_m

                if rng.random() < c_win:
                    c_payout  = c_dist * (c_bet / c_pool_no)
                    con_pnl_m = c_payout - c_bet
                else:
                    con_pnl_m = -c_bet

        d_con  += con_pnl_m
        d_rake += rake + con_rake_m
        market_pnls.append(base_pnl_m + con_pnl_m + rake + con_rake_m)

    # Carryover: unused budget (up to cap)
    unused          = max(0.0, capital_budget - deployed)
    max_carryover   = daily_limit * cfg.carryover_cap_pct
    new_carryover   = min(unused, max_carryover)

    return (
        d_base, d_con, d_rake, market_pnls,
        new_carryover, deployed, capital_budget,
        day_crowd_volume, harvest_triggered_this_day, new_cumulative_volume,
    )


# ── Full v2 simulation run ────────────────────────────────────────────────────

def run_simulation_v2(cfg: SimConfigV2) -> Dict[str, Any]:
    rng = random.Random(cfg.seed)

    baseline_rake = 0.025
    vol_factor = (
        (baseline_rake / cfg.rake_pct) ** cfg.rake_volume_elasticity
        if cfg.rake_pct > 0 else 2.0
    )
    eff_vol = cfg.crowd_volume_mean * vol_factor

    cap_base  = cap_con = cap_rake = cap_floor = cfg.starting_capital
    nav_base  = [cap_base]
    nav_con   = [cap_con]
    nav_rake  = [cap_rake]
    nav_floor = [cap_floor]

    day_results:  List[Dict] = []
    all_pnls:     List[float] = []
    d_rakes:      List[float] = []
    d_cons:       List[float] = []
    d_bases:      List[float] = []

    carryover          = 0.0
    harvest_mode       = False
    cumulative_volume  = 0.0

    for day in range(cfg.num_days):
        eff_edge = max(0.0, cfg.edge_discount * (1.0 - cfg.sophistication_decay * day))
        phase    = _get_phase(day, cfg.num_days)

        (
            d_base, d_con, d_rake, mkt_pnls,
            new_carryover, deployed, budget,
            day_crowd_vol, harvest_triggered, cumulative_volume,
        ) = _simulate_day_v2(
            day=day, current_nav=cap_rake, cfg=cfg, rng=rng,
            effective_edge=eff_edge, effective_crowd_vol_mean=eff_vol,
            carryover=carryover, harvest_mode=harvest_mode,
            cumulative_volume=cumulative_volume,
        )

        carryover    = new_carryover
        if harvest_triggered:
            harvest_mode = True

        cap_base  += d_base
        cap_con   += d_base + d_con
        cap_rake  += d_base + d_con + d_rake
        cap_floor += d_rake

        nav_base.append(cap_base)
        nav_con.append(cap_con)
        nav_rake.append(cap_rake)
        nav_floor.append(cap_floor)

        all_pnls.extend(mkt_pnls)
        d_bases.append(d_base)
        d_cons.append(d_con)
        d_rakes.append(d_rake)

        utilization_pct = (deployed / budget * 100) if budget > 0 else 0.0

        day_results.append({
            "day":                 day + 1,
            "phase":               phase,
            "effective_edge":      round(eff_edge, 4),
            "base_pnl":            d_base,
            "contrarian_pnl":      d_con,
            "rake_income":         d_rake,
            "total_pnl":           d_base + d_con + d_rake,
            "capital_deployed":    round(deployed, 2),
            "capital_budget":      round(budget, 2),
            "utilization_pct":     round(utilization_pct, 1),
            "carryover":           round(carryover, 2),
            "harvest_mode":        harvest_mode,
            "cumulative_volume":   round(cumulative_volume, 0),
        })

    def _stats(vals: List[float]) -> Dict:
        n    = len(vals)
        mean = sum(vals) / n if n else 0.0
        var  = sum((v - mean) ** 2 for v in vals) / n if n > 1 else 0.0
        return {"mean": mean, "std": math.sqrt(var), "total": sum(vals)}

    def _summary(final: float) -> Dict:
        ret = (final - cfg.starting_capital) / cfg.starting_capital * 100
        return {
            "final_nav":       final,
            "total_return_pct": ret,
            "total_pnl":        final - cfg.starting_capital,
            "profitable":       final > cfg.starting_capital,
        }

    return {
        "engine":      "v2",
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
            "harvest_triggered": harvest_mode,
            "final_cumulative_volume": cumulative_volume,
        },
    }
