"""
ChainAdapter — pinned interface contract (T1).

The real contract ABI isn't available yet ("coming soon"). To make "the ABI arrives
later as a swap-in, not a blocker" actually true rather than aspirational, this
interface is pinned NOW: both PlaceholderChainAdapter (below) and the eventual
real web3.py-backed implementation must satisfy the exact same four methods. Nothing
in decision.py, risk_guard.py, or daemon.py may depend on anything beyond this
contract.

`get_tx_status` is the one method beyond the design doc's pinned four — it's added
here because RiskGuard's restart reconciliation (T10) needs a way to re-check a
transaction's outcome independently of the place_bet() call that originally
submitted it (e.g. after the daemon crashes mid-flight). A real implementation would
back it with `w3.eth.get_transaction_receipt`; it's a plain chain read, not a new
piece of trading logic, so it doesn't expand the interface's actual scope.

`get_market_outcome` is a second bolt-on, same justification: realized P&L (for the
dashboard and logs/performance.jsonl) requires knowing which side actually won once
a market settles, which is a plain post-close contract read a real ChainAdapter
would serve from settlement state — not new trading logic either.
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Tuple

from .rpc_client import RateLimitedRpcClient

MarketId = str

TX_CONFIRMED = "confirmed"
TX_REVERTED = "reverted"
TX_UNCONFIRMED = "unconfirmed"  # submitted, no confirmation observed before close


@dataclass(frozen=True)
class TxResult:
    tx_hash: str
    status: str  # TX_CONFIRMED | TX_REVERTED | TX_UNCONFIRMED
    submitted_at: float


class ChainAdapter(Protocol):
    async def discover_markets(self) -> list[MarketId]:
        """Return the currently active/open market ids to poll."""
        ...

    async def get_pool_sizes(self, market_id: MarketId) -> Tuple[float, float]:
        """Return (yes_pool, no_pool) in USDC-equivalent units."""
        ...

    async def get_close_timestamp(self, market_id: MarketId) -> int:
        """Return 0 while the market is still open — this platform has no
        automatic time lock; betting closes only when the operator calls
        closeBetting(), which is also the moment this becomes nonzero (the
        timestamp betting closed at). This is NOT a future close time to
        count down to — there's no advance signal for when a market will
        close, so daemon.py's firing decision doesn't gate on one (see its
        module docstring's Option C note)."""
        ...

    async def place_bet(self, market_id: MarketId, side: str, amount_usdc: float) -> TxResult:
        """Submit a bet. May block briefly for confirmation given Monad's sub-second
        finality, but must return TX_UNCONFIRMED rather than hang indefinitely if
        confirmation doesn't land before the market's close."""
        ...

    async def get_tx_status(self, tx_hash: str) -> TxResult:
        """Re-check a previously submitted tx's outcome (restart reconciliation)."""
        ...

    async def get_market_outcome(self, market_id: MarketId) -> Optional[bool]:
        """Return True if YES won, False if NO won, or None if the market hasn't
        settled yet. Only meaningful after the market's close_ts has passed."""
        ...


class PlaceholderChainAdapter:
    """Stand-in for the real ABI. Simulates plausible pool dynamics and tx outcomes
    so the pipeline (connect -> poll -> detect -> size -> fire -> log) is fully
    exercisable end-to-end before the real contract lands."""

    def __init__(
        self,
        rpc: RateLimitedRpcClient,
        num_markets: int = 5,
        market_lifetime_s: float = 120.0,
        seed: int = 7,
    ):
        self._rpc = rpc
        self._rng = random.Random(seed)
        self._market_lifetime_s = market_lifetime_s
        self._markets: Dict[MarketId, dict] = {}
        self._tx_log: Dict[str, TxResult] = {}
        # Populated when a market expires (moved out of _markets), so
        # get_market_outcome can still answer for a market that's no longer active.
        self._resolved_outcomes: Dict[MarketId, bool] = {}
        for _ in range(num_markets):
            self._spawn_market()

    def _spawn_market(self) -> None:
        market_id = str(uuid.uuid4())[:8]
        now = time.time()
        bias = self._rng.uniform(0.5, 0.95)
        base = self._rng.uniform(50.0, 500.0)
        self._markets[market_id] = {
            "yes_pool": base * bias,
            "no_pool": base * (1.0 - bias),
            # Hidden internal close time — used only to decide when THIS
            # placeholder simulates closeBetting() having been called. Never
            # exposed directly; get_close_timestamp() below returns 0 until
            # this passes, matching the real contract's no-advance-signal
            # behavior (see that method and the ChainAdapter interface note).
            "_true_close_ts": now + self._rng.uniform(30.0, self._market_lifetime_s),
            "bias": bias,
            # True outcome is drawn independently of crowd bias at spawn time and
            # hidden until close — this is the whole premise a contrarian bet is
            # betting on (the crowd's bias doesn't track the true probability).
            "true_outcome_yes": self._rng.random() < 0.5,
        }

    async def discover_markets(self) -> list[MarketId]:
        async def _discover() -> list[MarketId]:
            now = time.time()
            expired = [mid for mid, m in self._markets.items() if m["_true_close_ts"] <= now]
            for mid in expired:
                self._resolved_outcomes[mid] = self._markets[mid]["true_outcome_yes"]
                del self._markets[mid]
                self._spawn_market()
            return list(self._markets.keys())

        return await self._rpc.call(_discover)

    async def get_pool_sizes(self, market_id: MarketId) -> Tuple[float, float]:
        async def _get() -> Tuple[float, float]:
            m = self._markets[market_id]
            drift = self._rng.uniform(-2.0, 4.0)
            if self._rng.random() < m["bias"]:
                m["yes_pool"] = max(0.0, m["yes_pool"] + drift)
            else:
                m["no_pool"] = max(0.0, m["no_pool"] + drift)
            return m["yes_pool"], m["no_pool"]

        return await self._rpc.call(_get)

    async def get_close_timestamp(self, market_id: MarketId) -> int:
        async def _get() -> int:
            m = self._markets[market_id]
            true_close = m["_true_close_ts"]
            if time.time() >= true_close:
                return int(true_close)  # simulates closeBetting() having fired
            return 0  # still open — no advance signal, matching the real contract

        return await self._rpc.call(_get)

    async def place_bet(self, market_id: MarketId, side: str, amount_usdc: float) -> TxResult:
        async def _place() -> TxResult:
            tx_hash = f"0xplaceholder{uuid.uuid4().hex[:16]}"
            submitted_at = time.time()
            # Simulate Monad's sub-second finality: usually confirms fast.
            outcome_roll = self._rng.random()
            if outcome_roll < 0.90:
                status = TX_CONFIRMED
            elif outcome_roll < 0.97:
                status = TX_REVERTED
            else:
                status = TX_UNCONFIRMED
            result = TxResult(tx_hash=tx_hash, status=status, submitted_at=submitted_at)
            self._tx_log[tx_hash] = result
            return result

        return await self._rpc.call(_place)

    async def get_tx_status(self, tx_hash: str) -> TxResult:
        async def _status() -> TxResult:
            return self._tx_log.get(
                tx_hash, TxResult(tx_hash=tx_hash, status=TX_UNCONFIRMED, submitted_at=0.0)
            )

        return await self._rpc.call(_status)

    async def get_market_outcome(self, market_id: MarketId) -> Optional[bool]:
        async def _outcome() -> Optional[bool]:
            return self._resolved_outcomes.get(market_id)

        return await self._rpc.call(_outcome)
