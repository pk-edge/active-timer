"""Rate limiters for the unauthenticated credential endpoints.

Two layers:
• SlidingWindowLimiter — in-process, zero dependencies. Per-replica budget.
• DistributedLimiter   — Redis fixed-window (INCR+EXPIRE) so the budget is
  shared across ALL replicas/workers; falls back to its embedded in-process
  limiter whenever Redis is unavailable or errors (fail-open to the same
  behavior we shipped before Redis was involved — never to "unlimited").
• MultiWindowLimiter   — the global API limiter's engine: several fixed-window
  buckets evaluated in ONE atomic Redis round-trip (Lua), same key namespace
  (`tt:rl:`, every key carries a TTL), same never-unlimited local fallback.
"""
import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass

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


# ── Multi-bucket engine for the global API rate limiter ───────────────────────

@dataclass(frozen=True)
class Bucket:
    """One fixed-window counter to charge for a request.

    `gate=False` marks an org-level bucket (e.g. per company): it is charged
    ONLY when every gate bucket passed, so one user hammering their own limit
    (their rejected requests still count against THEIR buckets) can never burn
    the whole company's shared budget.
    """
    name: str          # policy name, e.g. "burst" / "read" / "company"
    key: str           # identity inside the policy, e.g. "u:<uuid>"
    limit: int
    window: int        # seconds
    gate: bool = True


@dataclass(frozen=True)
class BucketResult:
    name: str
    limit: int
    count: int         # hits in the current window INCLUDING this request
    reset: int         # whole seconds until the window rolls over (>= 1)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)

    @property
    def exceeded(self) -> bool:
        return self.count > self.limit


# KEYS = counter keys (gate buckets first); ARGV[1] = number of gate keys,
# ARGV[2..1+N] = TTLs, ARGV[2+N..1+2N] = limits. INCR + EXPIRE are atomic here,
# so a crash between them can never leave an immortal counter.
_CONSUME_LUA = """
local n_gate = tonumber(ARGV[1])
local n = #KEYS
local over = false
local out = {}
for i = 1, n do
  local k = KEYS[i]
  if i <= n_gate then
    local c = redis.call('INCR', k)
    if c == 1 then redis.call('EXPIRE', k, tonumber(ARGV[1 + i])) end
    if c > tonumber(ARGV[1 + n + i]) then over = true end
    out[i] = c
  else
    if over then
      out[i] = tonumber(redis.call('GET', k) or '0')
    else
      local c = redis.call('INCR', k)
      if c == 1 then redis.call('EXPIRE', k, tonumber(ARGV[1 + i])) end
      out[i] = c
    end
  end
end
return out
"""


class MultiWindowLimiter:
    """Atomic multi-bucket fixed-window limiter: Redis-shared, local fallback.

    Redis is consulted with a short per-call timeout and guarded by a circuit
    breaker: after `redis_fail_threshold` CONSECUTIVE Redis errors the limiter
    serves from its in-process counters for `redis_retry_secs` before probing
    again, so a black-holed Redis can never add its socket timeout to every
    request. A single failed call (pool-exhaustion / timeout under a burst)
    only falls back to local counters for THAT call and does not open the
    breaker; any success resets the streak, and after a trip one more failure
    re-opens it immediately (half-open probe). In-process
    counters are per-worker (so with N workers the effective budget while
    Redis is down is N x the limit) — wider than the Redis-backed budget but
    never unlimited.
    """

    def __init__(self, redis_retry_secs: float = 5.0, redis_timeout: float = 0.25,
                 max_local_keys: int = 100_000, redis_fail_threshold: int = 3):
        self._retry = redis_retry_secs
        self._timeout = redis_timeout
        self._fail_threshold = max(1, redis_fail_threshold)
        self._fails = 0                    # consecutive Redis failures
        self._max_local = max_local_keys
        self._local: dict[str, tuple[int, float]] = {}     # key -> (count, expires_wall)
        self._redis_down_until = 0.0
        self._script = None
        self._script_client = None

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _client():
        from app.core.RedisClient import get_client
        return get_client()

    @property
    def redis_active(self) -> bool:
        return self._client() is not None and time.monotonic() >= self._redis_down_until

    @staticmethod
    def _slot(b: Bucket, now: float) -> tuple[str, int]:
        idx = int(now // b.window)
        reset = max(1, math.ceil((idx + 1) * b.window - now))
        return f"tt:rl:g:{b.name}:{b.key}:{idx}", reset

    # -- public ----------------------------------------------------------------
    async def consume(self, buckets: list[Bucket]) -> list[BucketResult]:
        """Charge one request against every bucket; return per-bucket results.

        Gate buckets are always charged; non-gate (org) buckets only when no
        gate bucket exceeded (see Bucket).
        """
        now = time.time()
        slots = [self._slot(b, now) for b in buckets]
        order = sorted(range(len(buckets)), key=lambda i: not buckets[i].gate)  # gates first
        counts: list[int] | None = None

        r = self._client()
        if r is not None and time.monotonic() >= self._redis_down_until:
            try:
                counts = await asyncio.wait_for(
                    self._consume_redis(r, buckets, slots, order), self._timeout)
                self._fails = 0
            except Exception as exc:  # noqa: BLE001 — any Redis failure => local
                self._fails += 1
                if self._fails >= self._fail_threshold:
                    self._redis_down_until = time.monotonic() + self._retry
                    log.warning("rate_limit: redis failed x%d (%s) — local counters for %.0fs",
                                self._fails, type(exc).__name__, self._retry)
                else:
                    log.debug("rate_limit: redis call failed (%s) — local for this call only",
                              type(exc).__name__)
        if counts is None:
            counts = self._consume_local(buckets, slots, order, now)

        return [BucketResult(b.name, b.limit, counts[i], slots[i][1])
                for i, b in enumerate(buckets)]

    # -- backends --------------------------------------------------------------
    async def _consume_redis(self, r, buckets, slots, order) -> list[int]:
        if self._script is None or self._script_client is not r:
            self._script = r.register_script(_CONSUME_LUA)
            self._script_client = r
        keys = [slots[i][0] for i in order]
        ttls = [buckets[i].window + 1 for i in order]
        limits = [buckets[i].limit for i in order]
        n_gate = sum(1 for b in buckets if b.gate)
        raw = await self._script(keys=keys, args=[n_gate, *ttls, *limits])
        counts = [0] * len(buckets)
        for pos, i in enumerate(order):
            counts[i] = int(raw[pos])
        return counts

    def _consume_local(self, buckets, slots, order, now) -> list[int]:
        counts = [0] * len(buckets)
        over = False
        for i in order:
            b = buckets[i]
            key, reset = slots[i]
            cur = self._local.get(key)
            c = cur[0] if cur else 0
            if b.gate or not over:
                c += 1
                self._local[key] = (c, now + reset + 1)
            if b.gate and c > b.limit:
                over = True
            counts[i] = c
        if len(self._local) > self._max_local:
            self._prune_local(now)
        return counts

    def _prune_local(self, now: float) -> None:
        """Bound memory under attack-scale key cardinality: drop expired
        windows first, then (still over) the oldest-inserted half. Losing a
        counter only ever errs toward allowing, and only past 100k live keys."""
        for k in [k for k, (_, exp) in self._local.items() if exp <= now]:
            self._local.pop(k, None)
        if len(self._local) > self._max_local:
            for k in list(self._local.keys())[: len(self._local) // 2]:
                self._local.pop(k, None)
