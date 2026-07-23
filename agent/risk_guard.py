"""
RiskGuard — the agent's persisted safety state (T5, T6, T9, T10).

Everything that must survive a daemon restart lives here and ONLY here: exposure-cap
accounting (per-bet + cumulative), the launch timestamp LaunchClock is built from,
and bet-per-market history. A restart that loses any of these makes them
unenforceable, so all three are persisted together in one state file.

Concurrency model (T9): every per-market asyncio polling task can call into
RiskGuard at any time, but the state file has exactly one writer — an internal
asyncio.Queue plus one dedicated `_writer_loop` task. Public methods never touch
`self._state` or the file directly; they enqueue a request and await its result.
This makes "check cap + check already-bet + reserve" a single atomic operation
(no other task's request can interleave inside it), eliminating the concurrent-
write-corruption risk entirely rather than just reducing its likelihood — and
avoids a TOCTOU race where two market tasks could both pass a cap check before
either one's reservation is recorded.

Restart reconciliation (T10): a bet is recorded "in_flight" BEFORE its transaction
is submitted (record_reservation), not after. If the daemon dies between submitting
a tx and recording its outcome, that market is stuck "in_flight" on disk. On the
next startup, reconcile_on_startup() must resolve every in_flight bet against chain
state before the daemon resumes normal polling — otherwise the agent could either
double-bet a market it already fired on, or carry an unrecorded exposure.

Circuit breaker (T5): a "failure" is a revert OR a tx submitted but never confirmed
before the market's close — both are treated identically, not as a separate
"unknown" bucket excluded from the trip count, because an unconfirmed-by-close tx is
exactly as unsafe to keep firing through as a revert.

Exposure caps (T6): per-bet ceiling bounds single-mistake blast radius; cumulative
lifetime total bounds aggregate capital at risk across many bets. Both are checked
on every reservation. Once capital is reserved for a bet, it stays counted against
the lifetime cap even if that bet later fails — the ledger tracks capital that was
put at risk, not a live "available balance", which is the conservative (safer)
choice for a control whose entire purpose is bounding worst-case exposure.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional

from .launch_clock import LaunchClock

STATUS_IN_FLIGHT = "in_flight"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED = "failed"


@dataclass
class BetRecord:
    market_id: str
    side: str
    amount_usdc: float
    status: str = STATUS_IN_FLIGHT
    tx_hash: Optional[str] = None
    submitted_at: float = 0.0
    resolved_at: Optional[float] = None


@dataclass
class ReservationResult:
    approved: bool
    reason: str


@dataclass
class _State:
    launch_ts: float
    cumulative_exposure_usdc: float = 0.0
    consecutive_tx_failures: int = 0
    bets: Dict[str, BetRecord] = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "_State":
        bets = {mid: BetRecord(**b) for mid, b in d.get("bets", {}).items()}
        return cls(
            launch_ts=d["launch_ts"],
            cumulative_exposure_usdc=d.get("cumulative_exposure_usdc", 0.0),
            consecutive_tx_failures=d.get("consecutive_tx_failures", 0),
            bets=bets,
        )


class RiskGuard:
    def __init__(
        self,
        state_path: str,
        per_bet_cap_usdc: float,
        lifetime_cap_usdc: float,
        circuit_breaker_threshold: int,
    ):
        self._state_path = Path(state_path)
        self._per_bet_cap_usdc = per_bet_cap_usdc
        self._lifetime_cap_usdc = lifetime_cap_usdc
        self._circuit_breaker_threshold = circuit_breaker_threshold

        self._state: Optional[_State] = None
        self._queue: "asyncio.Queue[tuple]" = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._state = self._load_or_init()
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        if self._writer_task is not None:
            await self._queue.join()
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

    def _load_or_init(self) -> _State:
        if self._state_path.exists():
            raw = json.loads(self._state_path.read_text())
            return _State.from_json(raw)
        state = _State(launch_ts=time.time())
        self._persist(state)
        return state

    def _persist(self, state: _State) -> None:
        """Atomic write-temp-then-rename — belt-and-suspenders alongside the
        single-writer queue, so even a crash mid-write can't leave a torn file."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state.to_json(), indent=2))
        os.replace(tmp_path, self._state_path)

    # ── public, read-only (safe without the queue: no compound check-then-act) ──

    @property
    def launch_clock(self) -> LaunchClock:
        assert self._state is not None, "RiskGuard.start() not called"
        return LaunchClock(launch_ts=self._state.launch_ts)

    def is_market_bet(self, market_id: str) -> bool:
        assert self._state is not None, "RiskGuard.start() not called"
        return market_id in self._state.bets

    def circuit_breaker_tripped(self) -> bool:
        assert self._state is not None, "RiskGuard.start() not called"
        return self._state.consecutive_tx_failures >= self._circuit_breaker_threshold

    # ── public, mutating (routed through the single writer) ────────────────────

    async def check_and_reserve(self, market_id: str, side: str, amount_usdc: float) -> ReservationResult:
        return await self._submit(("reserve", market_id, side, amount_usdc))

    async def record_tx_submitted(self, market_id: str, tx_hash: str) -> None:
        await self._submit(("attach_tx", market_id, tx_hash))

    async def resolve_bet(self, market_id: str, status: str, tx_hash: Optional[str] = None) -> None:
        assert status in (STATUS_CONFIRMED, STATUS_FAILED)
        await self._submit(("resolve", market_id, status, tx_hash))

    async def reconcile_on_startup(self, chain_adapter) -> None:
        """Must be called before the daemon resumes normal per-market polling."""
        assert self._state is not None, "RiskGuard.start() not called"
        in_flight = [
            (mid, b) for mid, b in self._state.bets.items() if b.status == STATUS_IN_FLIGHT
        ]
        for market_id, bet in in_flight:
            if not bet.tx_hash:
                # Crashed before a tx_hash was ever obtained — ambiguous outcome,
                # counts as a failure per the circuit-breaker's failure definition.
                await self.resolve_bet(market_id, STATUS_FAILED)
                continue
            tx_result = await chain_adapter.get_tx_status(bet.tx_hash)
            resolved_status = STATUS_CONFIRMED if tx_result.status == "confirmed" else STATUS_FAILED
            await self.resolve_bet(market_id, resolved_status, bet.tx_hash)

    # ── single-writer internals ──────────────────────────────────────────────

    async def _submit(self, request: tuple) -> Any:
        loop = asyncio.get_event_loop()
        result_future: "asyncio.Future" = loop.create_future()
        await self._queue.put((request, result_future))
        return await result_future

    async def _writer_loop(self) -> None:
        while True:
            (request, result_future) = await self._queue.get()
            try:
                result = self._apply(request)
                self._persist(self._state)
                if not result_future.done():
                    result_future.set_result(result)
            except Exception as exc:  # noqa: BLE001 — must not crash the sole writer
                if not result_future.done():
                    result_future.set_exception(exc)
            finally:
                self._queue.task_done()

    def _apply(self, request: tuple) -> Any:
        assert self._state is not None
        op = request[0]

        if op == "reserve":
            _, market_id, side, amount_usdc = request
            return self._apply_reserve(market_id, side, amount_usdc)

        if op == "attach_tx":
            _, market_id, tx_hash = request
            bet = self._state.bets[market_id]
            bet.tx_hash = tx_hash
            return None

        if op == "resolve":
            _, market_id, status, tx_hash = request
            return self._apply_resolve(market_id, status, tx_hash)

        raise ValueError(f"unknown RiskGuard writer op: {op!r}")

    def _apply_reserve(self, market_id: str, side: str, amount_usdc: float) -> ReservationResult:
        assert self._state is not None

        if self.circuit_breaker_tripped():
            return ReservationResult(False, "circuit breaker tripped")

        if market_id in self._state.bets:
            return ReservationResult(False, "market already bet this cycle")

        if amount_usdc > self._per_bet_cap_usdc:
            return ReservationResult(False, "exceeds per-bet cap")

        if self._state.cumulative_exposure_usdc + amount_usdc > self._lifetime_cap_usdc:
            return ReservationResult(False, "exceeds cumulative lifetime cap")

        self._state.bets[market_id] = BetRecord(
            market_id=market_id,
            side=side,
            amount_usdc=amount_usdc,
            status=STATUS_IN_FLIGHT,
            submitted_at=time.time(),
        )
        self._state.cumulative_exposure_usdc += amount_usdc
        return ReservationResult(True, "reserved")

    def _apply_resolve(self, market_id: str, status: str, tx_hash: Optional[str]) -> None:
        assert self._state is not None
        bet = self._state.bets[market_id]
        bet.status = status
        bet.resolved_at = time.time()
        if tx_hash:
            bet.tx_hash = tx_hash

        if status == STATUS_FAILED:
            self._state.consecutive_tx_failures += 1
        else:
            self._state.consecutive_tx_failures = 0
