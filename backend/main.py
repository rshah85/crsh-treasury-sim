from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sim import SimConfig, run_simulation

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
    fan_bias_mean: float = 0.70
    fan_bias_std: float = 0.12
    contrarian_threshold: float = 0.70
    contrarian_activation_prob: float = 0.50
    contrarian_pool_min: float = 50.0
    contrarian_pool_max: float = 150.0
    contrarian_win_prob_low: float = 0.30
    contrarian_win_prob_high: float = 0.40
    rake_pct: float = 0.025
    seed: int = 42


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/simulate")
def simulate(req: SimRequest):
    cfg = SimConfig(**req.model_dump())
    return run_simulation(cfg)
