"""Tiny in-process sliding-window rate limiter.

Per-replica (no Redis dependency): with N replicas an attacker gets N× the
budget, which is still a complete brute-force stop at these limits. Used for
the credential endpoints — everything else is authenticated.
"""
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, max_hits: int, window_secs: float, max_keys: int = 50_000):
        self.max_hits = max_hits
        self.window = window_secs
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque:
        dq = self._hits.get(key)
        if dq is None:
            dq = self._hits[key] = deque()
        while dq and dq[0] <= now - self.window:
            dq.popleft()
        return dq

    def blocked(self, key: str) -> bool:
        """True when the key has exhausted its budget (read-only check)."""
        return len(self._prune(key, time.monotonic())) >= self.max_hits

    def hit(self, key: str) -> None:
        """Record one event against the key."""
        now = time.monotonic()
        dq = self._prune(key, now)
        dq.append(now)
        # Bound total memory: drop the oldest tracked keys under attack-scale
        # cardinality. Losing a counter only ever errs toward allowing.
        if len(self._hits) > self.max_keys:
            for k in list(self._hits.keys())[: len(self._hits) - self.max_keys]:
                self._hits.pop(k, None)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
