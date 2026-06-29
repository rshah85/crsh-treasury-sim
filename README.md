# CRSH Treasury Simulator

A simulator for the CRSH community-owned treasury model: a pari-mutuel
prediction market where a treasury seeds every market with a symmetric
base allocation plus a contrarian overlay that leans into the minority
side as crowd imbalance grows, harvesting the behavioral tendency of fans
to overbet favorites.

- `/backend` — Python simulation engine (`sim.py`) exposed via FastAPI (`main.py`)
- `/frontend` — React dashboard (Vite + Recharts)

## How it works

Each simulated market:
1. Generates random crowd betting volume, skewed toward a "favorite" side
   by a configurable `fan_bias` parameter.
2. The treasury allocates a symmetric seed to both sides, plus a contrarian
   overlay sized by how imbalanced the crowd's betting is and a
   `contrarian_aggressiveness` scalar.
3. The market resolves independently of betting volume (favorite wins with
   probability `favorite_win_prob`) — this gap between true odds and crowd
   sentiment is the inefficiency the treasury harvests.
4. A rake is taken off the pool; the treasury receives its configured share.
5. Treasury P&L (pool payout +/- stake, plus rake share) accumulates across
   all markets in an epoch. NAV is recalculated at the end of each epoch.

## Backend setup

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

This exposes:
- `GET /api/health` — health check
- `POST /api/simulate` — run a simulation, body is the config (see `SimRequest` in `main.py`), returns NAV curve, per-epoch breakdown, and per-market P&L.

## Frontend setup

```bash
cd frontend
npm install
npm run dev
```

By default the frontend calls the backend at `http://localhost:8000`. Override
with a `VITE_API_URL` env var (see `frontend/.env`).

The dashboard lets you adjust all simulation parameters (starting capital,
markets per epoch, number of epochs, rake %, seed size, contrarian
aggressiveness, fan bias, etc.) and re-run the simulation live. It shows:

- Treasury NAV over time (line chart)
- Per-epoch fee income vs. contrarian edge harvested (stacked bar chart)
- Distribution of individual market P&L (histogram), illustrating how
  variance shrinks in aggregate even though single markets are noisy
- Summary stats: final NAV, total return, total fee income, total
  contrarian edge, markets simulated, % of markets profitable

## Notes

- The simulation is deterministic given a `seed` — same inputs always
  produce the same output, which makes parameter sweeps reproducible.
- `max_exposure_pct` caps the treasury's stake on either side of any single
  market as a percentage of current capital, preventing the treasury from
  dominating a market even as it grows.
