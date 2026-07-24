"""
Web3ChainAdapter — the real ChainAdapter implementation (T1's "swap-in" arriving),
backed by web3.py against the deployed UsdcPoolPredictionMarket contract
(agent/usdc-pool-abi.json).

Implements exactly the same interface PlaceholderChainAdapter does (see
agent/chain_adapter.py) — discover_markets, get_pool_sizes, get_close_timestamp,
place_bet, get_tx_status, get_market_outcome — so nothing in decision.py,
risk_guard.py, or daemon.py changes. PlaceholderChainAdapter is kept, not deleted:
it's still what --backtest and dashboard/demo runs use when no real RPC/contract/
keystore is configured (see daemon.py's adapter selection).

Design decisions worth calling out:

- **Approve+call, not permit, and approved ONCE at startup, not per-bet.** The
  design doc listed "single tx vs. approve+call" as a blocking open question
  because it determines whether the T-4s firing floor is feasible. The ABI
  confirms `bet()` pulls funds via a standard ERC20 allowance (there's also a
  `betWithPermit` path, but that requires EIP-2612 signature support from the
  deployed USDC token, which isn't confirmed). Resolving this pragmatically:
  `connect()` checks the current allowance once at startup and submits a single
  `approve()` transaction if it's insufficient — entirely OUTSIDE the hot firing
  path. Every live `bet()` call during the T-4s..T-10s window is then exactly
  ONE transaction, same as the placeholder assumed.
- **Approval is bounded to `lifetime_cap_usdc`, not "infinite approve."** Infinite
  ERC20 approval is a common anti-pattern; capping it to the same number
  RiskGuard already treats as the hard ceiling on total capital at risk means the
  on-chain allowance itself can't let this signer move more than that ceiling,
  even in a hypothetical RiskGuard bug — defense in depth, not just redundant.
- **Gas price and nonce are fetched fresh per bet, but gas LIMIT is a fixed
  config value, not estimated per-bet.** `estimate_gas` is itself an RPC round
  trip; skipping it keeps the firing-window hot path to a bounded, known number
  of calls (get_pool_sizes, get_close_timestamp, get_transaction_count, gas_price,
  send_raw_transaction, then receipt polling) rather than adding a data-dependent
  one. The fixed limit is a placeholder needing real calibration on testnet — see
  docs/graduation_gate.md.
- **Status enum ordinals: confirmed authoritatively by CRSH (Lucas) as
  `Open=0, Resolved=1, Cancelled=2` — there is no Closed state.** Matches what
  empirical sampling had already found (a 60-market random sample plus
  targeted lookups showed status=1 correlating with real, non-default
  `winningOption` + real stakes, i.e. Resolved). The only status value ever
  observed as anything but Resolved/Cancelled was Open — for markets whose
  lockTime had already passed, see the discover_markets() note below for why
  that's expected and handled, not a 4th state.
- **Market discovery: a small sliding window at the tip, re-checked every
  cycle — not a full rescan, and not a one-shot cursor either.** Confirmed by
  CRSH (Lucas): market ids are sequential and the newest is always
  `nextMarketId() - 1`, and this platform is round-based — markets open and
  close every few minutes tied to live gaming streams. A full
  `0..nextMarketId()-1` sweep (5+ minutes per pass at ~11 real calls/sec
  against this RPC's actual latency) is far too slow to catch short-lived
  rounds. An earlier fix tried a monotonic "classify each id exactly once,
  ever" cursor, but that has its own gap: a round short enough to open AND
  close within the time it takes discover_markets to reach it would get
  classified as already-expired on its one and only look, and never
  reconsidered. `discover_markets` now instead re-examines a small fixed
  window — the last `discovery_window` ids (default 5), i.e.
  `nextMarketId()-discovery_window .. nextMarketId()-1` — on EVERY call. Ids
  confirmed Resolved/Cancelled/past-lockTime while still inside the window are
  cached in `_known_terminal` so they aren't re-fetched every cycle for as
  long as they remain in-window; ids that age out of the window (superseded
  by newer ones) are simply never looked at again either way. This bounds
  cost to a small, constant number of calls per cycle regardless of total
  market history, and — unlike the one-shot cursor — keeps re-checking each
  in-window id every cycle until it's confirmed terminal, so a market isn't
  written off from a single unlucky-timing snapshot. The accepted tradeoff,
  per Lucas's guidance: a market that was already open and outside the window
  at daemon startup (or that somehow falls behind the window without ever
  being confirmed terminal) won't be picked up — bounded to whatever the
  window doesn't cover, not an unbounded bug.
- **`get_market_outcome` only reports Resolved markets.** A Cancelled market
  means funds are refundable via `claim()`, not a win/loss — there's no "someone
  won" outcome to report, and this adapter doesn't implement the claim flow
  (realizing an actual payout is a separate follow-on; agent/pnl.py's
  `compute_bet_pnl` is a modeled estimate for logging/dashboard purposes, not a
  stand-in for calling `claim()` and reading the real transfer).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional, Set, Tuple

from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound

from .chain_adapter import MarketId, TX_CONFIRMED, TX_REVERTED, TX_UNCONFIRMED, TxResult
from .erc20_abi import ERC20_ABI
from .key_custody import KeyCustody
from .rpc_client import RateLimitedRpcClient

# Confirmed by CRSH (Lucas) — see the "Status enum ordinals" note above.
STATUS_OPEN = 0
STATUS_RESOLVED = 1
STATUS_CANCELLED = 2
_TERMINAL_STATUSES = {STATUS_RESOLVED, STATUS_CANCELLED}

DEFAULT_DISCOVERY_WINDOW = 5


class Web3ChainAdapterError(Exception):
    pass


class Web3ChainAdapter:
    def __init__(
        self,
        rpc: RateLimitedRpcClient,
        rpc_url: str,
        contract_address: str,
        abi_path: str,
        key_custody: KeyCustody,
        approve_target_usdc: float,
        bet_gas_limit: int = 200_000,
        approve_gas_limit: int = 100_000,
        tx_confirmation_timeout_s: float = 3.0,
        tx_poll_interval_s: float = 0.25,
        discovery_window: int = DEFAULT_DISCOVERY_WINDOW,
    ):
        if not rpc_url:
            raise Web3ChainAdapterError("rpc_url is required for Web3ChainAdapter")

        self._rpc = rpc
        self._key_custody = key_custody
        self._approve_target_usdc = approve_target_usdc
        self._bet_gas_limit = bet_gas_limit
        self._approve_gas_limit = approve_gas_limit
        self._tx_confirmation_timeout_s = tx_confirmation_timeout_s
        self._tx_poll_interval_s = tx_poll_interval_s
        self._discovery_window = discovery_window

        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))

        if not AsyncWeb3.is_address(contract_address):
            raise Web3ChainAdapterError(f"invalid contract address: {contract_address!r}")
        self._contract_address = AsyncWeb3.to_checksum_address(contract_address)

        abi = json.loads(Path(abi_path).read_text())
        self.contract = self.w3.eth.contract(address=self._contract_address, abi=abi)

        # Populated by connect() — nothing below is usable before that.
        self._yes_value: Optional[int] = None
        self._no_value: Optional[int] = None
        self._min_bet_units: Optional[int] = None
        self._usdc_decimals: Optional[int] = None
        self._usdc_contract = None
        self._chain_id: Optional[int] = None
        # Ids confirmed Resolved/Cancelled/past-lockTime while still inside the
        # sliding discovery window — see the module docstring's "Market
        # discovery" note. Never needs seeding at startup: every
        # discover_markets() call computes its own window fresh from the
        # current nextMarketId(), so there's no cold-start scan to seed past.
        self._known_terminal: Set[int] = set()

    async def connect(self) -> None:
        """Must be called once before any other method. Fetches contract constants,
        the USDC token's decimals, and ensures the spend allowance is sufficient —
        submitting one `approve()` tx if it isn't. Everything here runs once at
        startup, deliberately outside the per-bet firing-window hot path."""
        self._chain_id = await self._rpc.call(self._get_chain_id)
        self._yes_value = await self._rpc.call(self.contract.functions.YES().call)
        self._no_value = await self._rpc.call(self.contract.functions.NO().call)
        self._min_bet_units = await self._rpc.call(self.contract.functions.MIN_BET_UNITS().call)

        usdc_address = await self._rpc.call(self.contract.functions.usdc().call)
        self._usdc_contract = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(usdc_address), abi=ERC20_ABI
        )
        self._usdc_decimals = await self._rpc.call(self._usdc_contract.functions.decimals().call)

        await self._ensure_allowance()

    async def _get_chain_id(self) -> int:
        return await self.w3.eth.chain_id

    def _to_raw(self, amount_usdc: float) -> int:
        return int(round(amount_usdc * (10 ** self._usdc_decimals)))

    def _to_usdc(self, amount_raw: int) -> float:
        return amount_raw / (10 ** self._usdc_decimals)

    async def _ensure_allowance(self) -> None:
        signer = self._key_custody.address
        current = await self._rpc.call(
            self._usdc_contract.functions.allowance(signer, self._contract_address).call
        )
        target_raw = self._to_raw(self._approve_target_usdc)
        if current >= target_raw:
            return

        tx_hash, status = await self._send_signed(
            self._usdc_contract.functions.approve(self._contract_address, target_raw),
            gas_limit=self._approve_gas_limit,
        )
        if status != TX_CONFIRMED:
            raise Web3ChainAdapterError(
                f"USDC approve() did not confirm before startup (tx {tx_hash}, status {status})"
            )

    async def _send_signed(self, contract_function, gas_limit: int) -> Tuple[str, str]:
        """Build, sign, submit, and poll-for-receipt a single contract call.
        Shared by _ensure_allowance and place_bet — the entire sign/send/poll
        lifecycle is identical regardless of which function is being called.

        Each real network call (get_transaction_count, gas_price,
        send_raw_transaction) goes through its own `self._rpc.call` — bundling
        all three under one token/semaphore acquisition would under-count the
        real load this places on the provider relative to what the shared rate
        limiter (T8) is meant to bound. See docs/queuing_latency_model.md's
        update note: this means one live `place_bet` now costs 3 real RPC calls
        here, not the 1 the placeholder-based model assumed."""
        signer = self._key_custody.address

        nonce = await self._rpc.call(self.w3.eth.get_transaction_count, signer, "pending")
        gas_price = await self._rpc.call(lambda: self.w3.eth.gas_price)
        tx = await contract_function.build_transaction(
            {
                "from": signer,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": self._chain_id,
            }
        )
        signed = self._key_custody.sign_transaction(tx)
        tx_hash_bytes = await self._rpc.call(self.w3.eth.send_raw_transaction, signed.raw_transaction)
        tx_hash = tx_hash_bytes.hex()

        status = await self._poll_for_status(tx_hash_bytes)
        return tx_hash, status

    async def _poll_for_status(self, tx_hash_bytes: bytes) -> str:
        deadline = time.monotonic() + self._tx_confirmation_timeout_s
        while time.monotonic() < deadline:
            try:
                receipt = await self._rpc.call(self.w3.eth.get_transaction_receipt, tx_hash_bytes)
            except TransactionNotFound:
                await self._sleep(self._tx_poll_interval_s)
                continue
            return TX_CONFIRMED if receipt["status"] == 1 else TX_REVERTED
        return TX_UNCONFIRMED

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _get_market(self, market_id: int):
        return await self._rpc.call(self.contract.functions.getMarket(market_id).call)

    # ── ChainAdapter interface ───────────────────────────────────────────────

    async def discover_markets(self) -> list:
        """Re-examines only the last `discovery_window` market ids relative to
        the CURRENT nextMarketId() tip, every single call — see the module
        docstring's "Market discovery" note. Ids confirmed terminal
        (Resolved/Cancelled/past-lockTime) while still in-window are cached in
        `_known_terminal` and skipped on later calls; ids that age out of the
        window without ever being confirmed terminal are simply never visited
        again either way, since the window only ever looks forward from
        `nextMarketId() - discovery_window`."""
        next_id = await self._rpc.call(self.contract.functions.nextMarketId().call)
        now = time.time()
        window_start = max(0, next_id - self._discovery_window)
        active: list = []
        for market_id in range(window_start, next_id):
            if market_id in self._known_terminal:
                continue
            market = await self._get_market(market_id)
            status, lock_time = market[0], market[3]
            if status == STATUS_OPEN and lock_time > now:
                active.append(str(market_id))
            else:
                self._known_terminal.add(market_id)
        return active

    async def get_pool_sizes(self, market_id: MarketId) -> Tuple[float, float]:
        market = await self._get_market(int(market_id))
        yes_stake, no_stake = market[4], market[5]
        return self._to_usdc(yes_stake), self._to_usdc(no_stake)

    async def get_close_timestamp(self, market_id: MarketId) -> int:
        market = await self._get_market(int(market_id))
        return int(market[3])  # lockTime

    async def place_bet(self, market_id: MarketId, side: str, amount_usdc: float) -> TxResult:
        submitted_at = time.time()
        amount_raw = self._to_raw(amount_usdc)

        if amount_raw < self._min_bet_units:
            # Would revert on-chain (TradeAmountTooSmall) — save the guaranteed-
            # to-fail round trip during the tight firing window.
            return TxResult(tx_hash="skipped:below-min-bet", status=TX_REVERTED, submitted_at=submitted_at)

        option = self._yes_value if side == "YES" else self._no_value
        tx_hash, status = await self._send_signed(
            self.contract.functions.bet(int(market_id), option, amount_raw),
            gas_limit=self._bet_gas_limit,
        )
        return TxResult(tx_hash=tx_hash, status=status, submitted_at=submitted_at)

    async def get_tx_status(self, tx_hash: str) -> TxResult:
        try:
            receipt = await self._rpc.call(self.w3.eth.get_transaction_receipt, tx_hash)
        except TransactionNotFound:
            return TxResult(tx_hash=tx_hash, status=TX_UNCONFIRMED, submitted_at=0.0)
        status = TX_CONFIRMED if receipt["status"] == 1 else TX_REVERTED
        return TxResult(tx_hash=tx_hash, status=status, submitted_at=0.0)

    async def get_market_outcome(self, market_id: MarketId) -> Optional[bool]:
        market = await self._get_market(int(market_id))
        status, winning_option = market[0], market[1]
        if status != STATUS_RESOLVED:
            return None
        return winning_option == self._yes_value
