"""
LaunchClock — treasury-level wall-clock phase/edge signal for the live agent.

sim_v2._get_phase(day, num_days) is a backtest-timeline construct: it's called once
per simulated day, before that day's per-market loop, so `day` and `num_days` are
both natural in a fixed-length backtest. A live agent has no "day N of 30" — it has
wall-clock time since it first went live. LaunchClock is the one component that
converts wall-clock time into the day-like input `_get_phase` and the effective-edge
formula both expect, so both keep reading a single, agreed-upon treasury-level
signal (one phase/edge value shared across all markets at any given moment), not a
per-market one.

Bug this fixes (caught during /plan-eng-review's outside-voice pass): phase wraps
every `horizon_days` (frac < 1/3 / frac < 2/3 against a wrapped day), but
sim_v2.run_simulation_v2's effective_edge formula is monotonic in raw `day` and never
wraps, because the backtest never runs past day 29. Feeding raw days_since_launch to
phase (wrapped) and effective_edge (unwrapped) makes them silently disagree after
~day 50: phase cycles back to full aggression every `horizon_days` while
effective_edge permanently collapses toward 0 forever, in a regime the backtest
never exercised. Both formulas must read the SAME wrapped day so they agree
indefinitely instead of diverging after one cycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ._sim_engine import _get_phase

DEFAULT_HORIZON_DAYS = 30


@dataclass
class LaunchClock:
    launch_ts: float
    horizon_days: int = DEFAULT_HORIZON_DAYS

    def days_since_launch(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - self.launch_ts) / 86400.0)

    def wrapped_day(self, now: float | None = None) -> float:
        """days_since_launch, wrapped into [0, horizon_days) — the SAME wrapped
        value phase() and effective_edge() both read, by construction."""
        return self.days_since_launch(now) % self.horizon_days

    def phase(self, now: float | None = None) -> int:
        wrapped = self.wrapped_day(now)
        # num_days=horizon_days reproduces the design doc's
        # frac = wrapped_day / (horizon_days - 1) exactly, via _get_phase's own
        # frac = day / max(num_days - 1, 1) — no threshold logic duplicated here.
        return _get_phase(wrapped, self.horizon_days)

    def effective_edge(
        self,
        edge_discount: float,
        sophistication_decay: float,
        now: float | None = None,
    ) -> float:
        wrapped = self.wrapped_day(now)
        return max(0.0, edge_discount * (1.0 - sophistication_decay * wrapped))
