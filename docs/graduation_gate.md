# Graduation Gate: Placeholder → Testnet → Mainnet (T13)

## Why this exists

The design doc's Dependencies section originally listed "a funded USDC treasury
wallet" as a bare dependency with no acceptance criteria — despite the
kill-switch/exposure-cap/circuit-breaker premise existing specifically for
real-money risk. This doc makes each stage transition an explicit, logged decision
with concrete pass/fail criteria, never a silent default.

## Real ABI update

The real `UsdcPoolPredictionMarket` ABI has arrived (`agent/usdc-pool-abi.json`,
contract `0x0A2fbF2Dbe08880D8D0d7f2F34B4CC3761f729b1` on Monad) and
`agent/web3_chain_adapter.py` now implements the ChainAdapter interface against
it. This resolves one previously-blocking Assignment question and surfaces one
new required testnet-verification step:

- **RESOLVED — gas sponsorship mechanism: neither relayer nor paymaster — gas is
  NOT sponsored.** The original design doc's Open Questions treated "relayer vs.
  paymaster" as a blocking unknown. Confirmed empirically during first live
  deployment: `Web3ChainAdapter`'s signer submits ordinary self-funded
  transactions (`gasPrice` from `w3.eth.gas_price`, signer pays from its own
  balance) and this is correct as-is — the actual gap was simply that the
  signer wallet held no native MON to pay gas with. No code changes needed;
  the wallet needs a MON balance funded directly, same as any normal EOA
  paying its own gas. Keep this funded going forward — an empty-gas-balance
  failure looks identical to a real revert in the daemon's logs/circuit
  breaker, so don't mistake a future recurrence of this for a contract issue.
- **RESOLVED — USDC bet-placement flow:** the contract pulls funds via a
  standard ERC20 allowance (`bet()`), not `betWithPermit`'s signature flow. The
  adapter approves once at startup (bounded to `lifetime_cap_usdc`, not
  "infinite approve") so every live `bet()` call is a single transaction — the
  T-4s floor's feasibility concern this question existed for is satisfied.
- **NEW — Status enum ordinals are assumed, not verified.** The ABI doesn't carry
  the integer values behind `enum UsdcPoolPredictionMarket.Status`.
  `web3_chain_adapter.py`'s `STATUS_OPEN=0/CLOSED=1/RESOLVED=2/CANCELLED=3`
  follow Solidity's default enum-ordering convention and the contract's error
  names, but this MUST be confirmed against a real `getMarket()` call on a
  known-status testnet market before Stage 2 — see the added gate item below.
- **NOT YET IMPLEMENTED — claim() / real settlement.** `get_market_outcome`
  reports the contract's resolved winning side, but doesn't call `claim()` to
  realize an actual payout. `agent/pnl.py`'s `compute_bet_pnl` remains a modeled
  estimate for logging/dashboard purposes, not authoritative settled P&L — a
  real claim-flow integration is a separate follow-on, not a v1 blocker.
- **NEW — discover_markets is a full range-scan.** No batch "list active
  markets" call exists in this ABI; discovery walks `0..nextMarketId()-1` each
  tick (terminal markets are cached and never re-fetched). Fine at today's
  market volume; will need a real index (backfilled from `MarketCreated`/
  `MarketBettingClosed` events) before volume grows meaningfully.

## Stage 1 → 2: Placeholder → Testnet

**Gate:** N (default 20) consecutive end-to-end decision cycles logged against
`PlaceholderChainAdapter` (or the real `Web3ChainAdapter` pointed at a Monad
testnet deployment, once one exists) with zero unhandled exceptions in the
daemon process, PLUS:

- **Status enum ordinals confirmed.** Call `getMarket()` against a market whose
  status you already know (e.g. one you just created and haven't closed) and
  verify the returned `status` value matches `STATUS_OPEN` in
  `web3_chain_adapter.py`. Do the same for a closed/resolved/cancelled market if
  available. Update the constants if the assumption was wrong — do not carry an
  unverified guess into mainnet.
- **Gas limits calibrated.** ~~Placeholder values; confirm actual gas usage~~
  DONE for `bet_gas_limit`: the original 200,000 placeholder was too low —
  every live bet reverted with "out of gas" (5 consecutive reverts, tripping
  the circuit breaker; see decisions/timeline for the date). Replaying the
  exact reverted calls via `estimate_gas` at the block before each landed
  showed 292,865-310,101 gas actually needed; `bet_gas_limit` is now 400,000
  (~30% headroom). `approve_gas_limit` (100,000) has been fine in practice.
  Re-verify both if the contract's `bet()` logic ever changes (e.g. new
  impact-reward calculations could shift gas usage).

A "cycle" here is one pass through `Daemon._market_loop`'s per-tick body for one
market — poll, decide, (skip or fire), log — as recorded by the structured JSON
event log (`agent/daemon.py`'s `_log`). Count `skip`, `waiting`, `firing`, and
`bet_result` events; a crash (process exit without a clean `stop()`) resets the
counter to zero, since the point is confidence the pipeline runs unattended, not
just that it ran once.

**How to check today:** run the daemon (`python -m agent.daemon`) and grep the
event log for the tally, e.g.:
```
python -m agent.daemon 2>&1 | tee run.log
grep -c '"event": "\(skip\|waiting\|firing\|bet_result\)"' run.log
```
No automated gate-check script exists yet — this is a manual count against the log,
which is sufficient for the "CFO/CTO watch a live run and understand it from logs
alone" bar this whole design is built around.

## Stage 2 → 3: Testnet → Mainnet

All three of the following are required — none is optional, and none may be
inferred or skipped silently:

1. **Explicit CTO sign-off.** A dated, recorded decision (e.g. in this repo's
   `~/.gstack/projects/.../decisions.jsonl` or equivalent), not an implied "it's
   been running fine so let's just point it at mainnet."
2. **Separate testnet and mainnet keystores.** `agent/key_custody.py`'s
   `KeyCustody` is already parameterized by `keystore_path` and
   `passphrase_env_var` (see `agent/config.py`'s `CRSH_KEYSTORE_PATH` /
   `CRSH_PASSPHRASE_ENV_VAR` env vars) specifically so testnet and mainnet run
   against two distinct keystore files with two distinct passphrases — never the
   same signer key on both networks. Verify the two `keystore_path` values differ
   before flipping any config to mainnet.
3. **A documented key-rotation / leaked-keystore rollback procedure**, written
   BEFORE any mainnet key is generated — not drafted reactively after an incident.
   At minimum this must cover: how to trigger the kill switch immediately
   (`agent/daemon.py`'s `check_kill_switch` — write the flag file at
   `CRSH_KILL_SWITCH_PATH`), how to rotate to a new keystore/address without losing
   `RiskGuard`'s exposure history, and who is authorized to execute the rotation.

## Non-negotiable across every stage transition

No stage advances silently. Each transition (placeholder→testnet,
testnet→mainnet) is a deliberate, dated, logged decision — the same standard this
design doc's own review process (`/plan-eng-review`, cross-model outside-voice
passes) was held to.
