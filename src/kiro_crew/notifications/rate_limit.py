"""Per-app rate limiting for notification pushes.

Token bucket per app: sustained rate of 30 notifications per 5 minutes with a
burst of 10. Deliberately coarse — the goal is stopping a buggy loop from
flooding the notification center, not fine-grained fairness. System channels
never pass through this limiter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 30 notifications per 5 minutes sustained, burst of 10.
RATE_LIMIT_TOKENS_PER_WINDOW = 30
RATE_LIMIT_WINDOW_SECS = 300.0
RATE_LIMIT_BURST = 10

_REFILL_RATE = RATE_LIMIT_TOKENS_PER_WINDOW / RATE_LIMIT_WINDOW_SECS  # tokens/sec


@dataclass
class _Bucket:
    tokens: float = float(RATE_LIMIT_BURST)
    last_refill: float = field(default_factory=time.monotonic)


class AppRateLimiter:
    """Token bucket per key. Single-threaded (asyncio event loop) use.

    Buckets are never evicted; growth is bounded because callers only pass
    names of installed, enabled apps (the push handler denies unknown apps
    with 403 before consulting the limiter), and each bucket is two floats.
    Entries for later-uninstalled apps linger harmlessly for the gateway's
    lifetime -- an accepted trade-off over eviction bookkeeping.

    One algorithm serves both the ingress per-app limiter and the egress
    per-transport bridge limiter: they are separate INSTANCES with their own
    policy, not two copies of the token bucket. ``burst``, ``tokens_per_window``
    and ``window_secs`` default to the ingress module constants, so an
    ``AppRateLimiter()`` with no arguments is byte-for-byte the ingress limiter;
    the bridge constructs ``AppRateLimiter(burst=5, tokens_per_window=20)`` for
    its own coarser egress budget.
    """

    def __init__(
        self,
        *,
        burst: int = RATE_LIMIT_BURST,
        tokens_per_window: int = RATE_LIMIT_TOKENS_PER_WINDOW,
        window_secs: float = RATE_LIMIT_WINDOW_SECS,
    ) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._burst = float(burst)
        self._refill_rate = tokens_per_window / window_secs  # tokens/sec

    def allow(self, key: str) -> bool:
        """Consume one token for *key*; False when the budget is exhausted."""
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, last_refill=now)
            self._buckets[key] = bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._refill_rate)
        bucket.last_refill = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    def refund(self, key: str) -> None:
        """Return one token to *key*'s bucket (capped at burst).

        For callers whose request consumed a token via :meth:`allow` but then
        failed without delivering anything -- the budget caps DELIVERED
        notifications, so non-delivering failures must not drain it.
        """
        bucket = self._buckets.get(key)
        if bucket is not None:
            bucket.tokens = min(self._burst, bucket.tokens + 1.0)
