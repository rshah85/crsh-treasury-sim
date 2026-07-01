from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List

from sim import SimConfig, run_simulation
from sim_v2 import SimConfigV2, run_simulation_v2

app = FastAPI(title="CRSH Treasury Simulator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SimRequest(BaseModel):
    starting_capital: float = 100_000.0
    num_days: int = 30
    markets_per_day: int = 50
    seed_per_side: float = 12.5
    crowd_volume_mean: float = 100.0
    crowd_volume_std: float = 25.0
    # Cluster bias
    num_clusters: int = 5
    fan_bias_mean: float = 0.55
    cluster_bias_std: float = 0.15
    market_bias_noise: float = 0.06
    # Edge model
    edge_discount: float = 0.25
    contrarian_threshold: float = 0.70
    contrarian_activation_prob: float = 0.50
    contrarian_pool_min: float = 50.0
    contrarian_pool_max: float = 150.0
    max_contrarian_exposure_pct: float = 0.30
    # Risk controls
    sophistication_decay: float = 0.02
    dynamic_exposure: bool = True
    daily_capital_limit_pct: float = 0.20
    # Rake
    rake_pct: float = 0.025
    rake_volume_elasticity: float = 0.5
    seed: int = 42


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/simulate")
def simulate(req: SimRequest):
    cfg = SimConfig(**req.model_dump())
    return run_simulation(cfg)


class SimRequestBoth(SimRequest):
    # V2-specific overrides (all optional — v2 defaults used when absent)
    phase1_threshold: float = 0.65
    phase2_threshold: float = 0.70
    phase3_threshold: float = 0.75
    phase1_kelly_scalar: float = 1.00
    phase2_kelly_scalar: float = 0.70
    phase3_kelly_scalar: float = 0.50
    max_kelly_fraction: float = 0.25
    carryover_cap_pct: float = 0.50
    volume_cap: float = 5_000_000.0
    harvest_trigger_pct: float = 0.80


@app.post("/api/simulate/both")
def simulate_both(req: SimRequestBoth):
    """Run v1 and v2 engines with the same shared params; return both results."""
    base = req.model_dump()

    # V1: pull only the fields SimConfig knows about
    v1_keys = set(SimConfig.__dataclass_fields__.keys())
    v1_params = {k: v for k, v in base.items() if k in v1_keys}
    v1 = run_simulation(SimConfig(**v1_params))

    # V2: uses all shared params + v2-specific ones
    v2_keys = set(SimConfigV2.__dataclass_fields__.keys())
    v2_params = {k: v for k, v in base.items() if k in v2_keys}
    v2 = run_simulation_v2(SimConfigV2(**v2_params))

    return {"v1": v1, "v2": v2}


@app.post("/api/sensitivity")
def sensitivity(req: SimRequest):
    """
    2-D sensitivity sweep over edge_discount × sophistication_decay.
    Returns a grid of total_return_pct (plus_rake layer) so the frontend
    can render a heatmap table.
    """
    edge_values  = [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]
    decay_values = [0.0, 0.02, 0.05, 0.1, 0.2, 0.5]

    base_params = req.model_dump()
    grid: List[List[float]] = []

    for ed in edge_values:
        row: List[float] = []
        for sd in decay_values:
            params = {**base_params, "edge_discount": ed, "sophistication_decay": sd}
            r = run_simulation(SimConfig(**params))
            row.append(round(r["summary"]["plus_rake"]["total_return_pct"], 2))
        grid.append(row)

    return {
        "edge_values":  edge_values,
        "decay_values": decay_values,
        "grid":         grid,
    }
