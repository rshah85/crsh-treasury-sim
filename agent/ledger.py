"""
Ledger — in-memory NAV/P&L bookkeeping for observability (dashboard, perf log).

Deliberately NOT persisted and NOT part of RiskGuard's restart-critical state:
RiskGuard's exposure ledger (agent/risk_guard.py) is the safety-critical source of
truth that gates whether a bet is allowed to fire and must survive a restart. This
Ledger only tracks realized P&L and open-position display state for humans watching
the demo — losing it on restart doesn't create any double-bet or unbounded-exposure
risk, so it doesn't carry RiskGuard's persistence and single-writer requirements.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Position:
    market_id: str
    side: str
    amount_usdc: float
    pool_yes_at_bet: float
    pool_no_at_bet: float
    opened_at: float = field(default_factory=time.time)


class Ledger:
    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.realized_pnl = 0.0
        self.closed_count = 0
        self.open_positions: Dict[str, Position] = {}

    @property
    def nav(self) -> float:
        return self.starting_capital + self.realized_pnl

    def open_position(
        self, market_id: str, side: str, amount_usdc: float, pool_yes: float, pool_no: float
    ) -> None:
        self.open_positions[market_id] = Position(
            market_id=market_id,
            side=side,
            amount_usdc=amount_usdc,
            pool_yes_at_bet=pool_yes,
            pool_no_at_bet=pool_no,
        )

    def close_position(self, market_id: str, pnl: float) -> None:
        self.open_positions.pop(market_id, None)
        self.realized_pnl += pnl
        self.closed_count += 1
