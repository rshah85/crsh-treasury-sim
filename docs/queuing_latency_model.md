# Queuing Latency Model vs. the T-4s Firing Floor (T12)

## Why this exists

The design doc's T-4s firing floor and the shared RPC client / single-writer
RiskGuard queue (agent/rpc_client.py, agent/risk_guard.py) were scoped independently
during `/office-hours` and `/plan-eng-review`. The outside-voice review flagged that
the same mechanisms that prevent rate-limit errors (T8) and state corruption (T9) are
the ones most likely to eat into the now-tight T-4s margin if many markets close at
once — this doc is that worst-case model, run before locking the floor.

## The two serialization points, modeled separately

### 1. Shared RPC client token bucket (`agent/rpc_client.py`)

Every chain read/write goes through one `RateLimitedRpcClient` with
`rate_per_sec=20.0` and a bucket capacity equal to `rate_per_sec` (i.e. it starts
full, holding a burst of 20 tokens, then refills at 20/sec). In the worst case, a
market that reaches its firing decision this tick needs up to 3 calls through the
shared client: `get_pool_sizes`, `get_close_timestamp`, `place_bet`.

For `N` markets whose close timestamps land in the same ~1s poll tick (a
"correlated close" burst), total demand is `3N` calls submitted at once. The
token-bucket-only queuing delay for the last call to get a token is:

```
worst_case_delay_s = max(0, (3N - rate_per_sec) / rate_per_sec)
```

| N (correlated closes) | Total calls | Token-bucket worst-case delay |
|---:|---:|---:|
| 5   | 15  | 0.00s |
| 10  | 30  | 0.50s |
| 20  | 60  | 2.00s |
| 30  | 90  | 3.50s |
| **46** | **138** | **5.90s** |
| 47  | 141 | 6.05s |
| 60  | 180 | 8.00s |
| 100 | 300 | 14.00s |

The firing window has a 6-second margin (T-10s down to T-4s). Under the current
defaults (`rpc_max_concurrent=8`, `rpc_rate_per_sec=20.0` in `agent/config.py`),
**the token bucket alone fully consumes that 6-second margin at ~46-47 correlated
market closes in the same tick** — before accounting for any real network RTT or
Monad's confirmation wait (~800ms-1s finality per the July 9, 2026 protocol upgrade,
per the design doc's Constraints).

### 2. RiskGuard's single-writer queue (`agent/risk_guard.py`)

`check_and_reserve` is intentionally fully serialized — one writer task, no
concurrency — because that's what eliminates the concurrent-write-corruption risk
entirely (T9). Its cost is a local disk write (write-temp-then-rename of a small
JSON file), not a network call, so it's far cheaper per-op but still purely additive
for the last market in a burst:

| N (correlated closes) | Serialization delay for the last market (~3ms/op) |
|---:|---:|
| 5   | 15 ms |
| 20  | 60 ms |
| 46  | 138 ms |
| 100 | 300 ms |

This is one to two orders of magnitude smaller than the RPC token-bucket delay at
the same N, so **the RPC client, not RiskGuard's writer queue, is the binding
constraint** on correlated-close capacity.

## Conclusion (placeholder-adapter estimate — superseded below)

Under current defaults, the T-4s floor holds for correlated-close bursts up to
roughly **N≈40 markets closing in the same 1-second tick**, with headroom to spare
before the rate limiter itself would blow the margin. Above that, the shared RPC
client's rate limit (not the single-writer queue) is what erodes the floor first.

This model only covers scheduling/queuing latency — it does NOT include real network
RTT or Monad's own confirmation latency, since those aren't measurable against a
placeholder adapter. **Before graduating past testnet (see docs/graduation_gate.md),
re-run this model with real observed RTT and confirmation-wait numbers substituted
in**, and re-tune `rpc_rate_per_sec` / `rpc_max_concurrent` in `agent/config.py` if
CRSH's actual expected concurrent-close volume is anywhere near the ~40 figure above.

## Update: real ABI changes the per-market call count

The estimate above assumed `place_bet` was ONE call through the shared client,
matching the placeholder adapter. `agent/web3_chain_adapter.py`'s real
`place_bet` decomposes into three separate real network calls
(`get_transaction_count`, `gas_price`, `send_raw_transaction` — each routed
through its own `rpc.call` deliberately, see that module's `_send_signed`
docstring, rather than being under-counted as one). So the actual per-firing-
market call count in the critical "get the bet submitted before close" path is
**5, not 3**: `get_pool_sizes`, `get_close_timestamp`, `get_transaction_count`,
`gas_price`, `send_raw_transaction`. (Receipt polling happens after submission —
the bet is already committed on-chain once `send_raw_transaction` returns, so
polling delay affects how fast the daemon learns the outcome, not whether the
bet made it into the firing window, and isn't counted here.)

Recomputing with 5 calls per market instead of 3:

| N (correlated closes) | Total calls | Token-bucket worst-case delay |
|---:|---:|---:|
| 5   | 25  | 0.25s |
| 10  | 50  | 1.50s |
| 20  | 100 | 4.00s |
| **24** | **120** | **5.00s** |
| 30  | 150 | 6.50s |
| 40  | 200 | 9.00s |
| 100 | 500 | 24.00s |

**The real correlated-close capacity is roughly N≈24-25 markets, not ~40-46** —
about 40% lower than the placeholder-based estimate, because bet placement now
costs 5x the RPC calls the earlier model assumed for that step. This is still
comfortably above CRSH's current market volume, but it's meaningfully tighter
than the original estimate and should be re-validated against real observed
concurrent-close volume before mainnet, per docs/graduation_gate.md. If actual
volume approaches this figure, the fix is either raising `rpc_rate_per_sec` (if
the RPC provider supports it) or reducing per-bet call count (e.g. caching a
recently-fetched `gas_price` across the burst instead of fetching fresh per bet
— not implemented, since gas price staleness during a real burst is a real risk
worth its own review before trading that latency for safety margin).
