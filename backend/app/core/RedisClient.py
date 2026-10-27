"""Shared async Redis client for response caches.

Separate from broadcaster.py's pub/sub connection so cache operations
(GET/SETEX) do not share a connection pool with the SSE subscription loop.
Uses a dedicated BLOCKING pool (REDIS_POOL_MAX_CONNECTIONS, default 32 per
worker): commands are short-lived and release their connection immediately, and
when a burst does exhaust the pool a caller waits up to REDIS_POOL_TIMEOUT_SECS
for a free connection instead of failing at once with "Too many connections"
(a non-blocking pool made one busy moment look like a dead server to the rate
limiter's circuit breaker). Pub/sub (broadcaster, auth-invalidation bus) uses
its own connections, never this pool.

Fails open: if Redis is unreachable at startup, get_client() returns None
and every cache call degrades to a live DB query with no error surface.
Redis errors after startup are swallowed by RedisCache callers.
"""
import asyncio
import logging
import re
from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

log = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None
_pool: Optional[aioredis.BlockingConnectionPool] = None


async def startup() -> None:
    global _redis, _pool
    if _redis is not None:
        return
    settings = get_settings()
    probe = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
    reachable = False
    try:
        await asyncio.wait_for(probe.ping(), timeout=3.0)
        reachable = True
    except Exception as exc:
        log.warning("redis_client: unreachable (%s) — response cache disabled", exc)
    finally:
        try:
            await probe.aclose()
        except Exception:
            pass

    if not reachable:
        return

    _pool = aioredis.BlockingConnectionPool.from_url(
        settings.REDIS_URL,
        max_connections=settings.REDIS_POOL_MAX_CONNECTIONS,
        timeout=settings.REDIS_POOL_TIMEOUT_SECS,
        decode_responses=False,
        health_check_interval=30,
        socket_keepalive=True,
        socket_connect_timeout=5,
        # Per-command ceiling: without it a black-holed Redis (TCP up, server
        # unresponsive) HANGS every caller instead of raising — and callers are
        # designed to fail open to a live DB read/local limiter on exceptions.
        socket_timeout=2,
    )
    _redis = aioredis.Redis(connection_pool=_pool)
    # Mask credentials: REDIS_URL may embed a password (redis://:pw@host).
    safe_url = re.sub(r"://[^@/]*@", "://***@", settings.REDIS_URL)
    log.info("redis_client: started (url=%s)", safe_url)


async def shutdown() -> None:
    global _redis, _pool
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception as exc:
            log.warning("redis_client: close error: %s", exc)
        _redis = None
    if _pool is not None:
        # A client built on an externally supplied pool does not own/close it.
        try:
            await _pool.disconnect()
        except Exception as exc:
            log.warning("redis_client: pool close error: %s", exc)
        _pool = None
    log.info("redis_client: stopped")


def get_client() -> Optional[aioredis.Redis]:
    return _redis
