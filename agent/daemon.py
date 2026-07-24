"""
Daemon — main asyncio orchestration for the live CRSH contrarian agent.

Ties together LaunchClock/decision.py (sizing), ChainAdapter (chain I/O),
RiskGuard (safety state), and the RPC client (shared rate limiting) into the
connect -> poll -> detect -> size -> fire -> log pipeline described in the design
doc. One asyncio task per active market, all sharing the same rate-limited RPC
client and the same RiskGuard instance, plus one resolution-polling task that
realizes P&L once a fired market's outcome is known (see agent/pnl.py,
agent/ledger.py, agent/perf_log.py).

T11 — kill switch fails safe: any error reading the flag file (permissions, disk,
transient I/O) is treated identically to the switch being triggered. This is why
`check_kill_switch` uses os.stat + explicit exception handling instead of
Path.exists() — exists() silently swallows OSErrors and returns False for both
"file genuinely absent" and "couldn't read it", which would make a read error
fail OPEN. That's the one behavior this control cannot have.

Option C (replacing the original T7 late-detection firing rule) — fire the moment
imbalance crosses the phase threshold, no countdown window. The original mistake
wasn't using `lockTime` as a real close time (confirmed by CRSH/Lucas: every market
has one — a short automatic round-lock at 60s/120s/300s from creation, or an ~8h
fallback for manually-closed markets) — it was gating FIRING on an artificial
T-8s..T-10s window before that countdown reaches zero, with a T-4s hard floor.
There's no reason to wait: `_market_loop` still uses `lockTime` to decide open
(`now < lockTime`, keep evaluating) vs. closed (`now >= lockTime`, stop), but fires
on the very first tick imbalance clears the phase threshold, regardless of how much
time remains. Short-window markets (`lockTime - now <= priority_window_s`, default
300s — the round-based ones tied to live gaming streams) get a faster poll cadence
(`priority_poll_interval_s`, default 0.5s) than the base `poll_interval_s`, so a
60-120s round doesn't go unchecked for a full second while imbalance is forming.

T3 — STALLED state: sustained poll failures (rate-limit errors and genuine RPC
outages look identical from here, which is exactly why the shared rate-limited
client exists) get a single loud STALLED transition log, then per-tick retries
log quietly until a success flips it back to RECOVERED — so a human watching logs
live can tell "broken" from "quiet" at a glance, per the stated demo bar.

Dashboard mode (--dashboard) takes over the terminal with a rich Live display
(agent/dashboard.py), which is why the structured event log is redirected to a
file in that mode instead of stdout — printing there would corrupt the live
redraw. Backtest mode (--backtest) doesn't construct a Daemon at all; it drives
decision.py/risk_guard.py directly against sim_v2-shaped synthetic data (see
agent/backtest.py) since there's no live chain or wall clock to poll.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from .chain_adapter import ChainAdapter, PlaceholderChainAdapter, TX_CONFIRMED
from .config import AgentConfig
from .decision import decide
from .key_custody import KeyCustody
from .ledger import Ledger
from .perf_log import PerformanceLog
from .pnl import compute_bet_pnl
from .risk_guard import RiskGuard, STATUS_CONFIRMED, STATUS_FAILED
from .rpc_client import RateLimitedRpcClient

LogSink = Callable[[dict], None]


def check_kill_switch(path: Path) -> tuple[bool, str]:
    """Fail-safe kill switch read (T11). Any error other than "file does not
    exist" is treated as the switch being triggered — never fail-open."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False, "clear"
    except OSError as exc:
        return True, f"kill-switch read error, failing safe: {exc}"
    return True, "kill-switch flag file present"


def stdout_log_sink(record: dict) -> None:
    print(json.dumps(record, default=str), flush=True)


def file_log_sink(path: str) -> LogSink:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _sink(record: dict) -> None:
        with open(log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    return _sink


class StallTracker:
    """Tracks consecutive poll failures and reports STALLED/RECOVERED transitions
    exactly once each, so routine per-tick retry noise doesn't drown out (or get
    confused with) a genuine sustained-outage signal."""

    def __init__(self, threshold: int):
        self._threshold = threshold
        self._consecutive_failures = 0
        self._stalled = False

    def record_success(self) -> Optional[str]:
        was_stalled = self._stalled
        self._consecutive_failures = 0
        self._stalled = False
        return "RECOVERED" if was_stalled else None

    def record_failure(self) -> Optional[str]:
        self._consecutive_failures += 1
        if not self._stalled and self._consecutive_failures >= self._threshold:
            self._stalled = True
            return "STALLED"
        return None

    @property
    def is_stalled(self) -> bool:
        return self._stalled


class Daemon:
    def __init__(
        self,
        config: AgentConfig,
        chain_adapter: Optional[ChainAdapter] = None,
        log_sink: Optional[LogSink] = None,
        key_custody: Optional[KeyCustody] = None,
    ):
        self.config = config
        self.rpc = RateLimitedRpcClient(
            max_concurrent=config.rpc_max_concurrent, rate_per_sec=config.rpc_rate_per_sec
        )
        self.chain_adapter = chain_adapter or PlaceholderChainAdapter(self.rpc)
        self.risk_guard = RiskGuard(
            state_path=config.risk_guard_state_path,
            per_bet_cap_usdc=config.per_bet_cap_usdc,
            lifetime_cap_usdc=config.lifetime_cap_usdc,
            circuit_breaker_threshold=config.circuit_breaker_threshold,
        )
        self.ledger = Ledger(starting_capital=config.sim.starting_capital)
        self.perf_log = PerformanceLog(config.performance_log_path)
        self._log_sink: LogSink = log_sink or stdout_log_sink

        # Each polling loop gets its OWN StallTracker: discover_markets and every
        # per-market poll hit the shared RPC client independently, and a success on
        # one must not mask sustained failures on another (e.g. discovery down
        # while already-known markets still poll fine is still worth a STALLED log).
        self._discovery_stall_tracker = StallTracker(config.stall_threshold)
        # May already be loaded and passed in (e.g. by _run_live, which needs a
        # signer address before it can construct a real Web3ChainAdapter) — in
        # that case start() just logs it instead of loading it a second time.
        self.key_custody: Optional[KeyCustody] = key_custody
        self._market_tasks: Dict[str, asyncio.Task] = {}
        self._kill_switch_active = False
        self._stop_event = asyncio.Event()

        # Latest known pool/timing snapshot per active market — updated by
        # _market_loop each tick, read only (never mutated) by the dashboard.
        self.market_snapshots: Dict[str, dict] = {}
        # Markets with a confirmed bet awaiting a chain-settled outcome.
        self._pending_resolution: Dict[str, dict] = {}

    def _log(self, event: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        self._log_sink(record)

    async def start(self, dashboard: bool = False) -> None:
        if self.key_custody is None and self.config.keystore_path:
            self.key_custody = KeyCustody(self.config.keystore_path, self.config.passphrase_env_var)
            self.key_custody.load()
            self._log("keystore_loaded", signer_address=self.key_custody.address)
        elif self.key_custody is not None:
            self._log("keystore_loaded", signer_address=self.key_custody.address)

        await self.risk_guard.start()
        self._log("reconciling_startup_state")
        await self.risk_guard.reconcile_on_startup(self.chain_adapter)

        self._log(
            "daemon_started",
            launch_ts=self.risk_guard.launch_clock.launch_ts,
            phase=self.risk_guard.launch_clock.phase(),
        )

        tasks = [
            asyncio.create_task(self._run_forever()),
            asyncio.create_task(self._resolution_loop()),
        ]
        if dashboard:
            from .dashboard import Dashboard

            tasks.append(asyncio.create_task(Dashboard(self).run()))
        self._background_tasks = tasks
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._market_tasks.values():
            task.cancel()
        for task in getattr(self, "_background_tasks", []):
            task.cancel()
        await self.risk_guard.stop()

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            triggered, reason = check_kill_switch(Path(self.config.kill_switch_path))
            if triggered != self._kill_switch_active:
                self._kill_switch_active = triggered
                self._log("kill_switch_" + ("engaged" if triggered else "cleared"), reason=reason)

            try:
                markets = await self.chain_adapter.discover_markets()
                transition = self._discovery_stall_tracker.record_success()
                if transition:
                    self._log(transition, source="discover_markets")
            except Exception as exc:  # noqa: BLE001 — poll failures must not crash the loop
                transition = self._discovery_stall_tracker.record_failure()
                self._log(transition or "poll_retry", source="discover_markets", error=str(exc))
                await asyncio.sleep(self.config.poll_interval_s)
                continue

            active_ids = set(markets)
            for stale_id in list(self.market_snapshots.keys()):
                if stale_id not in active_ids:
                    self.market_snapshots.pop(stale_id, None)

            for market_id in markets:
                existing = self._market_tasks.get(market_id)
                if existing is None or existing.done():
                    self._market_tasks[market_id] = asyncio.create_task(self._market_loop(market_id))

            await asyncio.sleep(self.config.poll_interval_s)

    async def _market_loop(self, market_id: str) -> None:
        # Own tracker per market task — see the note on _discovery_stall_tracker.
        stall_tracker = StallTracker(self.config.stall_threshold)

        while not self._stop_event.is_set():
            if self._kill_switch_active:
                self._log("skip", market_id=market_id, reason="kill switch active")
                await asyncio.sleep(self.config.poll_interval_s)
                continue

            try:
                yes_pool, no_pool = await self.chain_adapter.get_pool_sizes(market_id)
                lock_time = await self.chain_adapter.get_close_timestamp(market_id)
                transition = stall_tracker.record_success()
                if transition:
                    self._log(transition, market_id=market_id)
            except KeyError:
                self._log("market_closed", market_id=market_id, reason="no longer tracked by chain adapter")
                self.market_snapshots.pop(market_id, None)
                return
            except Exception as exc:  # noqa: BLE001
                transition = stall_tracker.record_failure()
                self._log(transition or "poll_retry", market_id=market_id, error=str(exc))
                await asyncio.sleep(self.config.poll_interval_s)
                continue

            now = time.time()
            seconds_remaining = lock_time - now

            # A market is bettable iff now < lockTime — every market has a
            # real lockTime (a short automatic round-lock, or an ~8h fallback
            # for manually-closed markets), never a "0 until closed" sentinel.
            # See the module docstring's Option C note.
            if seconds_remaining <= 0:
                self._log("market_closed", market_id=market_id)
                self.market_snapshots.pop(market_id, None)
                return

            # Short-window (round-based) markets get checked more often, so a
            # late-forming imbalance on a 60-120s round isn't missed by only
            # polling once a second.
            sleep_s = (
                self.config.priority_poll_interval_s
                if seconds_remaining <= self.config.priority_window_s
                else self.config.poll_interval_s
            )

            self.market_snapshots[market_id] = {
                "yes_pool": yes_pool,
                "no_pool": no_pool,
                "seconds_remaining": seconds_remaining,
                "updated_at": now,
            }

            if self.risk_guard.is_market_bet(market_id):
                await asyncio.sleep(sleep_s)
                continue

            if self.risk_guard.circuit_breaker_tripped():
                self._log("skip", market_id=market_id, reason="circuit breaker tripped",
                           seconds_remaining=round(seconds_remaining, 2))
                await asyncio.sleep(sleep_s)
                continue

            decision = decide(
                market_id, yes_pool, no_pool, self.config.sim, self.risk_guard.launch_clock, now
            )

            if decision.action == "skip":
                self._log(
                    "skip", market_id=market_id, reason=decision.reason, phase=decision.phase,
                    seconds_remaining=round(seconds_remaining, 2),
                )
                await asyncio.sleep(sleep_s)
                continue

            # No firing-window gate — fire as soon as imbalance crosses the
            # phase threshold. See the module docstring's Option C note.
            reservation = await self.risk_guard.check_and_reserve(
                market_id, decision.side, decision.amount_usdc
            )
            if not reservation.approved:
                self._log("skip", market_id=market_id, reason=reservation.reason,
                           seconds_remaining=round(seconds_remaining, 2))
                await asyncio.sleep(sleep_s)
                continue

            self._log(
                "firing", market_id=market_id, side=decision.side, amount_usdc=round(decision.amount_usdc, 2),
                phase=decision.phase, seconds_remaining=round(seconds_remaining, 2),
            )
            tx = await self.chain_adapter.place_bet(market_id, decision.side, decision.amount_usdc)
            await self.risk_guard.record_tx_submitted(market_id, tx.tx_hash)
            resolved_status = STATUS_CONFIRMED if tx.status == TX_CONFIRMED else STATUS_FAILED
            await self.risk_guard.resolve_bet(market_id, resolved_status, tx.tx_hash)
            self._log("bet_result", market_id=market_id, tx_hash=tx.tx_hash, status=tx.status)

            self.perf_log.log_bet_placed(
                market_id, decision.side, decision.amount_usdc, yes_pool, no_pool,
                decision.phase, self.ledger.realized_pnl,
            )

            if resolved_status == STATUS_CONFIRMED:
                self.ledger.open_position(market_id, decision.side, decision.amount_usdc, yes_pool, no_pool)
                self._pending_resolution[market_id] = {
                    "side": decision.side,
                    "amount_usdc": decision.amount_usdc,
                    "pool_yes_at_bet": yes_pool,
                    "pool_no_at_bet": no_pool,
                }
            else:
                self.perf_log.log_bet_failed(market_id, decision.side, decision.amount_usdc, tx.status)

            return  # max one contrarian bet per market

    async def _resolution_loop(self) -> None:
        """Polls for settlement on markets with a confirmed, unresolved bet and
        realizes P&L once known. Separate from _market_loop because a market's
        polling task exits the moment it fires (one bet per market) — resolution
        can happen well after that, so it needs its own long-lived loop."""
        while not self._stop_event.is_set():
            for market_id in list(self._pending_resolution.keys()):
                try:
                    outcome_yes = await self.chain_adapter.get_market_outcome(market_id)
                except Exception as exc:  # noqa: BLE001 — one bad lookup shouldn't wedge the loop
                    self._log("resolution_poll_retry", market_id=market_id, error=str(exc))
                    continue
                if outcome_yes is None:
                    continue

                pos = self._pending_resolution.pop(market_id)
                pnl = compute_bet_pnl(
                    pos["pool_yes_at_bet"], pos["pool_no_at_bet"], pos["side"],
                    pos["amount_usdc"], outcome_yes, self.config.sim.rake_pct,
                )
                self.ledger.close_position(market_id, pnl)
                self.perf_log.log_bet_resolved(
                    market_id, pos["side"], pos["amount_usdc"], outcome_yes, pnl, self.ledger.realized_pnl
                )
                self._log(
                    "position_resolved", market_id=market_id, pnl=round(pnl, 2),
                    running_pnl=round(self.ledger.realized_pnl, 2),
                )

            await asyncio.sleep(self.config.poll_interval_s)


async def _build_chain_adapter(config: AgentConfig):
    """Returns (chain_adapter, key_custody). Uses the real Web3ChainAdapter only
    when rpc_url, contract_address, AND a keystore are ALL explicitly configured
    — see agent/config.py's module docstring for why there's no partial/implicit
    activation. Otherwise falls back to PlaceholderChainAdapter, same as before
    this real-ABI integration existed."""
    if not (config.rpc_url and config.contract_address and config.keystore_path):
        return None, None

    from .web3_chain_adapter import Web3ChainAdapter

    key_custody = KeyCustody(config.keystore_path, config.passphrase_env_var)
    key_custody.load()

    rpc = RateLimitedRpcClient(
        max_concurrent=config.rpc_max_concurrent, rate_per_sec=config.rpc_rate_per_sec
    )
    approve_target = (
        config.approve_target_usdc if config.approve_target_usdc is not None else config.lifetime_cap_usdc
    )
    adapter = Web3ChainAdapter(
        rpc=rpc,
        rpc_url=config.rpc_url,
        contract_address=config.contract_address,
        abi_path=config.abi_path,
        key_custody=key_custody,
        approve_target_usdc=approve_target,
        bet_gas_limit=config.bet_gas_limit,
        approve_gas_limit=config.approve_gas_limit,
        tx_confirmation_timeout_s=config.tx_confirmation_timeout_s,
        tx_poll_interval_s=config.tx_poll_interval_s,
        discovery_window=config.discovery_window,
    )
    await adapter.connect()  # one-time YES/NO/decimals/allowance setup — never in the hot path
    return adapter, key_custody


async def _run_live(dashboard: bool) -> None:
    config = AgentConfig.from_env()
    if dashboard and not config.daemon_log_path:
        config.daemon_log_path = "logs/daemon_events.jsonl"

    log_sink = file_log_sink(config.daemon_log_path) if config.daemon_log_path else stdout_log_sink
    chain_adapter, key_custody = await _build_chain_adapter(config)
    daemon = Daemon(config, chain_adapter=chain_adapter, log_sink=log_sink, key_custody=key_custody)
    try:
        await daemon.start(dashboard=dashboard)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await daemon.stop()


def _parse_args(argv):
    import argparse

    parser = argparse.ArgumentParser(description="CRSH contrarian trading agent daemon")
    parser.add_argument(
        "--backtest", action="store_true",
        help="Replay sim_v2-shaped synthetic markets through decision.py/risk_guard.py "
             "instead of running the live daemon (see agent/backtest.py).",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Render a live rich terminal dashboard instead of printing the JSON event "
             "log to stdout (the event log is redirected to a file instead).",
    )
    parser.add_argument("--days", type=int, default=None, help="--backtest: override SimConfigV2.num_days")
    parser.add_argument("--seed", type=int, default=None, help="--backtest: override SimConfigV2.seed")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.backtest:
        from .backtest import run_backtest

        config = AgentConfig.from_env()
        if args.days is not None:
            config.sim.num_days = args.days
        if args.seed is not None:
            config.sim.seed = args.seed
        run_backtest(config)
        return

    asyncio.run(_run_live(dashboard=args.dashboard))


if __name__ == "__main__":
    main()
