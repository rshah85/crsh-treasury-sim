"""
Win rate by pool-size bucket, computed from the live performance log
(agent/perf_log.py's logs/performance.jsonl, or wherever CRSH_PERFORMANCE_LOG_PATH
points — /data/performance.jsonl on the deployed volume).

Exists to answer one question as real bets resolve: does MOMENTUM_WIN_PROB
(0.871, measured on the >=$200-pool subset of the historical backtest) hold up
in the sub-$200 pools decision.py now also fires on since MIN_POOL_USDC dropped
to $50, or does it degrade? See decision.py's MIN_POOL_USDC / MOMENTUM_WIN_PROB.

A "bet_placed" line carries the pool state at bet time; the matching
"bet_resolved" line (same market_id) carries the outcome. This script joins the
two rather than re-deriving pool size from anywhere else, so it reports exactly
what the agent saw when it fired.

Usage: python3 -m agent.pool_bucket_report [path]
  path defaults to $CRSH_PERFORMANCE_LOG_PATH, falling back to
  logs/performance.jsonl.
"""

from __future__ import annotations

import json
import os
import sys

POOL_BUCKET_THRESHOLD_USDC = 200.0


def _load(path: str) -> dict:
    markets: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            market_id = rec.get("market_id")
            if market_id is None:
                continue
            markets.setdefault(market_id, {})[rec["event"]] = rec
    return markets


def _report(markets: dict) -> None:
    buckets = {
        f"< ${POOL_BUCKET_THRESHOLD_USDC:.0f}": [],
        f">= ${POOL_BUCKET_THRESHOLD_USDC:.0f}": [],
    }
    unresolved = 0
    failed = 0

    for market_id, events in markets.items():
        placed = events.get("bet_placed")
        resolved = events.get("bet_resolved")
        if placed is None:
            continue
        if "bet_failed" in events:
            failed += 1
            continue
        if resolved is None:
            unresolved += 1
            continue

        total_pool = placed["pool_yes_at_bet"] + placed["pool_no_at_bet"]
        bucket = (
            f"< ${POOL_BUCKET_THRESHOLD_USDC:.0f}"
            if total_pool < POOL_BUCKET_THRESHOLD_USDC
            else f">= ${POOL_BUCKET_THRESHOLD_USDC:.0f}"
        )
        buckets[bucket].append(
            {
                "market_id": market_id,
                "total_pool": total_pool,
                "won": resolved["won"],
                "pnl": resolved["pnl"],
            }
        )

    print(f"Resolved bets: {sum(len(v) for v in buckets.values())}  "
          f"(unresolved/in-flight: {unresolved}, failed/reverted: {failed})")
    print()

    for label, bets in buckets.items():
        n = len(bets)
        if n == 0:
            print(f"{label}: no resolved bets yet")
            print()
            continue
        wins = sum(1 for b in bets if b["won"])
        total_pnl = sum(b["pnl"] for b in bets)
        avg_pool = sum(b["total_pool"] for b in bets) / n
        print(f"{label} pool at bet time:")
        print(f"  n={n}  win_rate={wins/n*100:.1f}%  ({wins}W / {n-wins}L)")
        print(f"  avg pool at bet: ${avg_pool:,.2f}")
        print(f"  total pnl: ${total_pnl:,.2f}   avg pnl/bet: ${total_pnl/n:,.2f}")
        print()


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = os.environ.get("CRSH_PERFORMANCE_LOG_PATH", "logs/performance.jsonl")

    if not os.path.exists(path):
        print(f"No performance log found at {path}")
        return

    markets = _load(path)
    _report(markets)


if __name__ == "__main__":
    main()
