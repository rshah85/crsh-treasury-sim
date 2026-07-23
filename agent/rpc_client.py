"""
Shared rate-limited RPC client.

Design constraint: one shared rate-limited RPC client (semaphore/token-bucket)
serving every per-market polling task, rather than each task hitting the endpoint
independently. Without this, N concurrent 1s-cadence pollers scale their combined
request rate with N, and a rate-limit error from the provider is indistinguishable
from a genuine RPC outage to the STALLED-detection logic in daemon.py — both look
like "the call failed." Centralizing here means only ONE component needs to reason
about the provider's real rate limit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class RateLimitedRpcClient:
    def __init__(self, max_concurrent: int = 8, rate_per_sec: float = 20.0):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._rate_per_sec = rate_per_sec
        self._capacity = max(rate_per_sec, 1.0)
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def _acquire_token(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._last_refill = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_sec)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._rate_per_sec
            await asyncio.sleep(wait_s)

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """Run one RPC-bound coroutine under the shared concurrency + rate limits."""
        await self._acquire_token()
        async with self._sem:
            return await fn(*args, **kwargs)
