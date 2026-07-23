"""
Bootstraps imports of backend/sim_v2.py so the live agent reuses the exact same
Kelly sizing and phase-schedule math validated by the treasury simulator, instead
of re-implementing it. backend/ is not a package (no __init__.py, deployed with
cwd=backend), so it isn't importable as `backend.sim_v2` from here — add it to
sys.path once, then import normally.
"""

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sim_v2 import SimConfigV2, _get_phase, _kelly_bet_size  # noqa: E402

__all__ = ["SimConfigV2", "_get_phase", "_kelly_bet_size"]
