"""Rate limiters for the unauthenticated credential endpoints.

Two layers:
• SlidingWindowLimiter — in-process, zero dependencies. Per-replica budget.
• DistributedLimiter   — Redis fixed-window (INCR+EXPIRE) so the budget is
  shared across ALL replicas/workers; falls back to its embedded in-process
  limiter whenever Redis is unavailable or errors (fail-open to the same
  behavior we shipped before Redis was involved — never to "unlimited").
"""
import logging
import time
from collections import deque

log = logging.getLogger(__name__)


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


class DistributedLimiter:
    """Redis-backed fixed-window limiter shared across replicas.

    Fixed window (INCR + EXPIRE) instead of a true sliding window: at
    brute-force-stopping limits the boundary imprecision is irrelevant, and it
    is one round-trip per operation. Keys live under `tt:rl:{name}:` and expire
    with the window, so abandoned attack keys cost nothing.

    Every operation ALSO updates the in-process limiter, which becomes the
    source of truth the moment Redis is down/unreachable — a Redis flap can
    therefore never open a brute-force hole wider than today's per-replica one.
    """

    def __init__(self, name: str, max_hits: int, window_secs: float):
        self.name = name
        self.max_hits = max_hits
        self.window = window_secs
        self._local = SlidingWindowLimiter(max_hits, window_secs)

    def _key(self, key: str) -> str:
        window_start = int(time.time() // self.window)
        return f"tt:rl:{self.name}:{key}:{window_start}"

    @staticmethod
    def _client():
        # Late import: RedisClient may not be started (tests/scripts) — get_client
        # simply returns None then and we run purely in-process.
        from app.core.RedisClient import get_client
        return get_client()

    async def blocked(self, key: str) -> bool:
        r = self._client()
        if r is not None:
            try:
                current = await r.get(self._key(key))
                if current is not None and int(current) >= self.max_hits:
                    return True
            except Exception as exc:
                log.warning("rate_limit[%s]: redis read failed (%s) — using local", self.name, exc)
        return self._local.blocked(key)

    async def hit(self, key: str) -> None:
        self._local.hit(key)
        r = self._client()
        if r is None:
            return
        try:
            k = self._key(key)
            count = await r.incr(k)
            if count == 1:
                await r.expire(k, int(self.window) + 1)
        except Exception as exc:
            log.warning("rate_limit[%s]: redis write failed (%s)", self.name, exc)

    async def reset(self, key: str) -> None:
        self._local.reset(key)
        r = self._client()
        if r is None:
            return
        try:
            await r.delete(self._key(key))
        except Exception as exc:
            log.warning("rate_limit[%s]: redis reset failed (%s)", self.name, exc)
