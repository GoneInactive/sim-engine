"""Per-API-key token bucket rate limiting (§8) — protects the book from a
runaway loop in a student notebook."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    def __init__(self, rate_per_second: float, burst: int):
        self.rate = rate_per_second
        self.burst = burst
        self.buckets: dict[str, _Bucket] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.burst, last_refill=now)
            self.buckets[key] = bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
        bucket.last_refill = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False
