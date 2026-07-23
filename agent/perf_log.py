"""
Structured per-bet performance log — logs/performance.jsonl.

Append-only JSONL, one line per event. A bet's lifecycle is TWO lines, never one
rewritten line: "bet_placed" when fired (outcome unknown yet, running_pnl is the
total BEFORE this bet) and "bet_resolved" once the market's outcome is known (this
bet's realized pnl and the UPDATED running total). A "bet_failed" line covers the
revert/unconfirmed case, where no market outcome ever applies.

Rewriting an earlier line to fill in "outcome when known" would reintroduce the
exact concurrent-write-corruption problem RiskGuard's single-writer queue (T9)
exists to solve, for a file that has no such queue. Two append-only lines, joined
by market_id, avoids that entirely. Readers who want one row per bet can group by
market_id: a market_id with only a "bet_placed" line is still open or in flight.

Each write is a single synchronous, non-yielding file append (open, write, close),
same reasoning as RiskGuard's in-memory reads being safe without a queue: no other
asyncio task can interleave inside a call with no `await` in it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class PerformanceLog:
    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, record: dict) -> None:
        line = json.dumps(record, default=str)
        with open(self._path, "a") as f:
            f.write(line + "\n")

    def log_bet_placed(
        self,
        market_id: str,
        side: str,
        amount_usdc: float,
        pool_yes_at_bet: float,
        pool_no_at_bet: float,
        phase: int,
        running_pnl: float,
    ) -> None:
        self._append(
            {
                "event": "bet_placed",
                "ts": round(time.time(), 3),
                "market_id": market_id,
                "side": side,
                "amount_usdc": round(amount_usdc, 4),
                "pool_yes_at_bet": round(pool_yes_at_bet, 4),
                "pool_no_at_bet": round(pool_no_at_bet, 4),
                "phase": phase,
                "outcome": None,
                "pnl": None,
                "running_pnl": round(running_pnl, 4),
            }
        )

    def log_bet_resolved(
        self,
        market_id: str,
        side: str,
        amount_usdc: float,
        outcome_yes: bool,
        pnl: float,
        running_pnl: float,
    ) -> None:
        self._append(
            {
                "event": "bet_resolved",
                "ts": round(time.time(), 3),
                "market_id": market_id,
                "side": side,
                "amount_usdc": round(amount_usdc, 4),
                "outcome": "YES" if outcome_yes else "NO",
                "won": (side == "YES") == outcome_yes,
                "pnl": round(pnl, 4),
                "running_pnl": round(running_pnl, 4),
            }
        )

    def log_bet_failed(self, market_id: str, side: str, amount_usdc: float, tx_status: str) -> None:
        self._append(
            {
                "event": "bet_failed",
                "ts": round(time.time(), 3),
                "market_id": market_id,
                "side": side,
                "amount_usdc": round(amount_usdc, 4),
                "tx_status": tx_status,
                "outcome": None,
                "pnl": 0.0,
            }
        )
