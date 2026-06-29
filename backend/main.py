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
    markets_per_epoch: int = 50
    num_epochs: int = 20
    rake_pct: float = 0.025
    treasury_rake_share: float = 0.5
    seed_per_side: float = 12.5
    contrarian_aggressiveness: float = 1.0
    fan_bias_mean: float = 0.75
    fan_bias_std: float = 0.1
    favorite_win_prob: float = 0.55
    crowd_volume_mean: float = 1000.0
    crowd_volume_std: float = 300.0
    max_exposure_pct: float = 0.05
    seed: int = 42


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/simulate")
def simulate(req: SimRequest):
    cfg = SimConfig(**req.model_dump())
    return run_simulation(cfg)
