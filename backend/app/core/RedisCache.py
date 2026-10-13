"""Async Redis response cache with stale-while-revalidate semantics.

Keys use the prefix 'tt:c:' to avoid collisions with the broadcaster's
pub/sub channel ('devices_updated').  Each entry is stored as orjson bytes:

    {"data": <response_payload>, "ts": <unix_float>}

Redis TTL is set to STALE_TTL seconds so entries self-expire.  Freshness is
checked in Python from the 'ts' field:

    age < FRESH_TTL  → fresh  (serve, no background work)
    age < STALE_TTL  → stale  (serve + fire background refresh)
    age ≥ STALE_TTL  → miss   (serve nothing; compute live)

Cache keys are tuples of arbitrary values.  frozensets and nested tuples are
SHA-256–hashed so Redis keys stay short regardless of fleet size.

AWS cost notes
──────────────
• A cache.t4g.micro (0.5 GB) is sufficient: widget/overview payloads are
  1–5 KB each, TTL=5 min, ~1 000 distinct (company × scope × params) entries
  → < 10 MB even at scale.
• Pub/sub (broadcaster) and cache share the same REDIS_URL endpoint; the
  separate connection pools prevent cache churn from blocking SSE delivery.
• decode_responses=False keeps values as bytes (orjson output) — no UTF-8
  decode overhead on every get.
"""
import hashlib
import logging
import time
import uuid
from typing import Any, Optional

import orjson

from app.core.RedisClient import get_client

log = logging.getLogger(__name__)

_PREFIX   = "tt:c:"

# Default TTLs — override per call-site via fresh_secs / stale_secs kwargs.
FRESH_TTL = 30    # seconds — home dashboard (polled every 30 s)
STALE_TTL = 300   # seconds — home dashboard stale window

# Analytics endpoints (tech, reports) — data changes slowly over multi-day windows.
FRESH_ANALYTICS = 120   # 2 minutes
STALE_ANALYTICS = 600   # 10 minutes

# Detail drill endpoints (per-host, per-app, per-category) — moderate cost.
FRESH_DETAIL = 60       # 1 minute
STALE_DETAIL = 300      # 5 minutes


def _build_key(key: tuple) -> str:
    """Convert a cache key tuple to a stable, compact Redis key string.

    Handles nested tuples and frozensets (device-ID sets) by hashing them.
    Plain strings and ints are written verbatim for readability in redis-cli.
    """
    def _part(v: Any) -> str:
        if isinstance(v, frozenset):
            digest = hashlib.sha256(
                ",".join(sorted(str(x) for x in v)).encode()
            ).hexdigest()[:20]
            return f"fs:{digest}"
        if isinstance(v, tuple):
            inner = ":".join(_part(x) for x in v)
            return f"({inner})"
        return str(v)

    raw = ":".join(_part(p) for p in key)
    if len(raw) > 220:
        raw = hashlib.sha256(raw.encode()).hexdigest()
    return _PREFIX + raw


async def cache_get(key: tuple) -> tuple[Optional[Any], bool]:
    """Return (data, is_stale).  Returns (None, False) on miss or Redis down.

    Freshness thresholds are stored inside the entry (set by cache_set) so
    callers that use different TTLs get the right stale/fresh classification
    without passing TTL params to every get() call.
    """
    r = get_client()
    if r is None:
        return None, False
    try:
        raw = await r.get(_build_key(key))
        if raw is None:
            return None, False
        entry = orjson.loads(raw)
        age = time.time() - entry["ts"]
        stale_secs = entry.get("ss", STALE_TTL)
        fresh_secs = entry.get("fs", FRESH_TTL)
        if age >= stale_secs:
            return None, False
        return entry["data"], age >= fresh_secs
    except Exception as exc:
        log.warning("redis_cache.get error: %s", exc)
        return None, False


async def cache_set(
    key: tuple,
    data: Any,
    *,
    fresh_secs: int = FRESH_TTL,
    stale_secs: int = STALE_TTL,
) -> None:
    """Store data in Redis.

    Redis TTL = stale_secs so entries auto-expire.  fresh_secs and stale_secs
    are embedded in the entry so cache_get can classify freshness without needing
    to receive the same TTL values again.
    """
    r = get_client()
    if r is None:
        return
    try:
        payload = orjson.dumps({"data": data, "ts": time.time(), "fs": fresh_secs, "ss": stale_secs})
        await r.setex(_build_key(key), stale_secs, payload)
    except Exception as exc:
        log.warning("redis_cache.set error: %s", exc)


async def cache_delete(key: tuple) -> None:
    """Explicitly evict a cache entry (e.g. after a destructive mutation)."""
    r = get_client()
    if r is None:
        return
    try:
        await r.delete(_build_key(key))
    except Exception as exc:
        log.warning("redis_cache.delete error: %s", exc)


# ── Single-flight ─────────────────────────────────────────────────────────────
# A cold cache key is normally requested by several callers at once: the Tech
# Utilization page mounts ten SWR calls, and changing a filter re-keys them
# simultaneously across every open tab. Without a guard each of those recomputes
# the same multi-second aggregation. SET NX gives us a cross-worker lock (an
# in-process asyncio.Lock would only collapse duplicates within one worker).
#
# The lock is strictly an optimisation: if Redis is down, or the holder dies, or
# the wait times out, every caller simply computes its own value as before. It
# must never be able to make a request fail or hang.

LOCK_TTL_SECS  = 30     # holder crash ceiling — must exceed a slow recompute
LOCK_WAIT_SECS = 5.0    # how long a loser waits for the winner's result
_LOCK_POLL     = 0.05
_LOCK_POLL_MAX = 0.25   # polling backs off to this, so a long wait stays cheap

# Compare-and-delete: only the holder that wrote the token may remove the lock.
# An unconditional DEL would let a holder whose TTL already expired delete the
# lock of the caller that legitimately took over after it.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _lock_name(key: tuple) -> str:
    return _PREFIX + "lock:" + _build_key(key)


async def try_acquire(key: tuple, ttl: int = LOCK_TTL_SECS):
    """Try to take the single-flight lock for `key`.

    Returns the holder's unique token (a truthy str) when this caller won, False
    when somebody else holds it, and True when Redis is unavailable — with no
    lock service the safe behaviour is "everyone computes", never "nobody
    computes". Pass the return value back to release(); True needs no release.
    """
    r = get_client()
    if r is None:
        return True
    token = uuid.uuid4().hex
    try:
        won = await r.set(_lock_name(key), token.encode(), nx=True, ex=ttl)
        return token if won else False
    except Exception as exc:
        log.warning("redis_cache.lock error: %s", exc)
        return True


async def release(key: tuple, token=None) -> None:
    """Release the lock taken by try_acquire. With the holder's `token` the
    delete is token-checked (atomic Lua compare-and-delete), so a holder whose
    lock already expired and was re-acquired by someone else cannot remove that
    caller's lock. `True` (the fail-open "no lock service" result) is a no-op;
    no token at all keeps the legacy unconditional DEL."""
    if token is True:
        return
    r = get_client()
    if r is None:
        return
    try:
        if token:
            await r.eval(_RELEASE_LUA, 1, _lock_name(key), str(token).encode())
        else:
            await r.delete(_lock_name(key))
    except Exception as exc:
        log.warning("redis_cache.unlock error: %s", exc)


async def wait_or_take(key: tuple, timeout: float = LOCK_WAIT_SECS, *,
                       check_failure: bool = False, ttl: int = LOCK_TTL_SECS):
    """Wait for the lock winner's value, or take over if the winner vanishes.

    Returns (data, token):
      (data, None)   the winner cached a value — use it
      (None, token)  the lock disappeared without a value (the winner was
                     cancelled / crashed / released after an error) and THIS
                     caller re-acquired it: compute, cache, then release(token)
      (None, None)   timed out, or — with check_failure — the winner recorded a
                     failure marker; the caller re-checks cache_get_failure() to
                     tell those apart and otherwise computes without the lock
    """
    import asyncio
    deadline = time.time() + timeout
    delay = _LOCK_POLL
    while time.time() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, _LOCK_POLL_MAX)
        data, _ = await cache_get(key)
        if data is not None:
            return data, None
        if check_failure and await cache_get_failure(key) is not None:
            return None, None
        token = await try_acquire(key, ttl=ttl)
        if token:
            # The lock was free. The value may have landed between our read and
            # the acquire (winners cache BEFORE releasing), so look once more.
            data, _ = await cache_get(key)
            if data is not None:
                await release(key, token)
                return data, None
            return None, token
    return None, None


async def wait_for(key: tuple, timeout: float = LOCK_WAIT_SECS, *, check_failure: bool = False):
    """Poll for the lock winner's value. Returns the data, or None on timeout.

    None means "compute it yourself" — a slow path, not an error. With
    check_failure=True the wait also ends early (returning None) the moment the
    winner records a failure marker for `key`, so losers don't sit out the whole
    timeout behind a query that already died; the caller re-checks
    cache_get_failure() to tell that apart from a plain timeout. (Callers that
    want to take over a vanished lock use wait_or_take instead.)
    """
    import asyncio
    deadline = time.time() + timeout
    delay = _LOCK_POLL
    while time.time() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, _LOCK_POLL_MAX)
        data, _ = await cache_get(key)
        if data is not None:
            return data
        if check_failure and await cache_get_failure(key) is not None:
            return None
    return None


# ── Negative caching of failures ──────────────────────────────────────────────
# A heavy aggregation that times out will time out again for the identical
# request, and every retry (the dashboard polls, users re-click, several tabs)
# burns another full statement_timeout of DB time. A short-lived marker per exact
# cache key lets the endpoint answer 503 immediately instead. Like the lock it is
# best-effort: with Redis down no marker is ever seen and requests just compute.

FAILURE_TTL = 30    # seconds an identical request is refused after a failure


async def cache_set_failure(key: tuple, ttl: int = FAILURE_TTL) -> None:
    """Record that computing `key` just failed, for `ttl` seconds."""
    r = get_client()
    if r is None:
        return
    try:
        await r.setex(_PREFIX + "fail:" + _build_key(key), ttl, b"1")
    except Exception as exc:
        log.warning("redis_cache.set_failure error: %s", exc)


async def cache_get_failure(key: tuple) -> Optional[int]:
    """Seconds (>= 1) until the failure marker for `key` expires, or None when
    there is no marker (or Redis is down)."""
    r = get_client()
    if r is None:
        return None
    try:
        name = _PREFIX + "fail:" + _build_key(key)
        if not await r.exists(name):
            return None
        ttl = await r.ttl(name)
        return max(int(ttl), 1) if ttl and ttl > 0 else 1
    except Exception as exc:
        log.warning("redis_cache.get_failure error: %s", exc)
        return None


async def cache_clear_failure(key: tuple) -> None:
    """Drop the failure marker (a later success supersedes it)."""
    r = get_client()
    if r is None:
        return
    try:
        await r.delete(_PREFIX + "fail:" + _build_key(key))
    except Exception as exc:
        log.warning("redis_cache.clear_failure error: %s", exc)
