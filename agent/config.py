"""
Agent configuration. Values marked "NEEDS CTO CONFIRMATION" are the ones the design
doc's Open Questions / The Assignment flag as required config with no default
proposed yet (circuit-breaker threshold, both exposure caps). The values below are
conservative placeholders so the pipeline is runnable end-to-end against the
placeholder ChainAdapter today — they are NOT a substitute for that conversation and
must not be carried into a testnet/mainnet graduation without it (see
docs/graduation_gate.md).

Deliberately no hardcoded default for `rpc_url` or `contract_address`, even though
both are now known (Monad mainnet RPC + the confirmed UsdcPoolPredictionMarket
deployment): daemon.py only switches from PlaceholderChainAdapter to the real
Web3ChainAdapter when rpc_url, contract_address, AND a keystore are ALL explicitly
configured. If one of those had a baked-in default, importing this module with an
otherwise-empty environment could silently flip the daemon into live-trading mode
the moment a keystore happened to be configured for an unrelated reason — that's
exactly the kind of silent dangerous default the kill switch and exposure caps
elsewhere in this codebase are designed to avoid. Set these explicitly via env vars
when you actually mean to go live:
    CRSH_RPC_URL=https://rpc.monad.xyz
    CRSH_CONTRACT_ADDRESS=0x0A2fbF2Dbe08880D8D0d7f2F34B4CC3761f729b1
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from ._sim_engine import SimConfigV2


@dataclass
class AgentConfig:
    sim: SimConfigV2 = field(default_factory=SimConfigV2)

    poll_interval_s: float = 1.0

    # T-8s..T-10s target window; hard floor T-4s (see The Assignment for the tx-flow
    # question this floor's feasibility depends on, and docs/queuing_latency_model.md
    # for why T-4s survives correlated market closes).
    fire_window_high_s: float = 10.0
    fire_window_low_s: float = 4.0

    # NEEDS CTO CONFIRMATION — placeholder values only.
    per_bet_cap_usdc: float = 500.0
    lifetime_cap_usdc: float = 20_000.0
    circuit_breaker_threshold: int = 5

    stall_threshold: int = 10

    rpc_max_concurrent: int = 8
    rpc_rate_per_sec: float = 20.0

    kill_switch_path: str = "agent/state/KILL_SWITCH"
    risk_guard_state_path: str = "agent/state/risk_guard_state.json"

    keystore_path: Optional[str] = None
    passphrase_env_var: str = "CRSH_SIGNER_PASSPHRASE"

    # Real chain wiring (agent/web3_chain_adapter.py) — no defaults, see module
    # docstring above. All three must be set for daemon.py to use the real
    # adapter instead of PlaceholderChainAdapter.
    rpc_url: Optional[str] = None
    contract_address: Optional[str] = None
    abi_path: str = "agent/usdc-pool-abi.json"

    # USDC spend approval is bounded to lifetime_cap_usdc, not "infinite approve"
    # — see web3_chain_adapter.py's module docstring. None means "use
    # lifetime_cap_usdc" (the default, computed in daemon.py's adapter wiring).
    approve_target_usdc: Optional[float] = None

    # Gas limits are fixed config, not estimated per-bet, to keep the firing-
    # window hot path to a bounded number of RPC calls. Placeholder values —
    # need real calibration on testnet before mainnet use.
    bet_gas_limit: int = 200_000
    approve_gas_limit: int = 100_000

    # Market discovery re-checks only the last discovery_window market ids
    # relative to nextMarketId() every cycle (round-based platform — markets
    # rotate every few minutes, per CRSH). See web3_chain_adapter.py's module
    # docstring.
    discovery_window: int = 5

    # How long place_bet waits for a receipt before returning TX_UNCONFIRMED
    # rather than hanging. Must stay well inside fire_window_low_s (the T-4s
    # floor) — see docs/queuing_latency_model.md.
    tx_confirmation_timeout_s: float = 2.0
    tx_poll_interval_s: float = 0.25

    # Per-bet structured performance log (see agent/perf_log.py). Always on.
    performance_log_path: str = "logs/performance.jsonl"

    # Structured daemon event log (the "skip"/"firing"/"STALLED"/etc. stream).
    # None means stdout (default). --dashboard mode redirects this to a file
    # automatically since it would otherwise corrupt the live rich display.
    daemon_log_path: Optional[str] = None

    @classmethod
    def from_env(cls) -> "AgentConfig":
        cfg = cls()
        cfg.poll_interval_s = float(os.environ.get("CRSH_POLL_INTERVAL_S", cfg.poll_interval_s))
        cfg.fire_window_high_s = float(os.environ.get("CRSH_FIRE_WINDOW_HIGH_S", cfg.fire_window_high_s))
        cfg.fire_window_low_s = float(os.environ.get("CRSH_FIRE_WINDOW_LOW_S", cfg.fire_window_low_s))
        cfg.per_bet_cap_usdc = float(os.environ.get("CRSH_PER_BET_CAP_USDC", cfg.per_bet_cap_usdc))
        cfg.lifetime_cap_usdc = float(os.environ.get("CRSH_LIFETIME_CAP_USDC", cfg.lifetime_cap_usdc))
        cfg.circuit_breaker_threshold = int(
            os.environ.get("CRSH_CIRCUIT_BREAKER_THRESHOLD", cfg.circuit_breaker_threshold)
        )
        cfg.stall_threshold = int(os.environ.get("CRSH_STALL_THRESHOLD", cfg.stall_threshold))
        cfg.kill_switch_path = os.environ.get("CRSH_KILL_SWITCH_PATH", cfg.kill_switch_path)
        cfg.risk_guard_state_path = os.environ.get(
            "CRSH_RISK_GUARD_STATE_PATH", cfg.risk_guard_state_path
        )
        cfg.keystore_path = os.environ.get("CRSH_KEYSTORE_PATH", cfg.keystore_path)
        cfg.passphrase_env_var = os.environ.get("CRSH_PASSPHRASE_ENV_VAR", cfg.passphrase_env_var)
        cfg.rpc_url = os.environ.get("CRSH_RPC_URL", cfg.rpc_url)
        cfg.contract_address = os.environ.get("CRSH_CONTRACT_ADDRESS", cfg.contract_address)
        cfg.abi_path = os.environ.get("CRSH_ABI_PATH", cfg.abi_path)
        if os.environ.get("CRSH_APPROVE_TARGET_USDC"):
            cfg.approve_target_usdc = float(os.environ["CRSH_APPROVE_TARGET_USDC"])
        cfg.bet_gas_limit = int(os.environ.get("CRSH_BET_GAS_LIMIT", cfg.bet_gas_limit))
        cfg.approve_gas_limit = int(os.environ.get("CRSH_APPROVE_GAS_LIMIT", cfg.approve_gas_limit))
        cfg.discovery_window = int(os.environ.get("CRSH_DISCOVERY_WINDOW", cfg.discovery_window))
        cfg.tx_confirmation_timeout_s = float(
            os.environ.get("CRSH_TX_CONFIRMATION_TIMEOUT_S", cfg.tx_confirmation_timeout_s)
        )
        cfg.tx_poll_interval_s = float(
            os.environ.get("CRSH_TX_POLL_INTERVAL_S", cfg.tx_poll_interval_s)
        )
        cfg.performance_log_path = os.environ.get(
            "CRSH_PERFORMANCE_LOG_PATH", cfg.performance_log_path
        )
        cfg.daemon_log_path = os.environ.get("CRSH_DAEMON_LOG_PATH", cfg.daemon_log_path)
        return cfg
