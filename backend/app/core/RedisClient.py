"""Shared async Redis client for response caches.

Separate from broadcaster.py's pub/sub connection so cache operations
(GET/SETEX) do not share a connection pool with the SSE subscription loop.
Uses a dedicated pool capped at 8 connections — cache hits are short-lived
commands that release connections immediately, so 8 is generous even under
heavy dashboard load.

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


async def startup() -> None:
    global _redis
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

    _redis = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=False,
        health_check_interval=30,
        socket_keepalive=True,
        socket_connect_timeout=5,
        # Per-command ceiling: without it a black-holed Redis (TCP up, server
        # unresponsive) HANGS every caller instead of raising — and callers are
        # designed to fail open to a live DB read/local limiter on exceptions.
        socket_timeout=2,
        max_connections=8,
    )
    # Mask credentials: REDIS_URL may embed a password (redis://:pw@host).
    safe_url = re.sub(r"://[^@/]*@", "://***@", settings.REDIS_URL)
    log.info("redis_client: started (url=%s)", safe_url)


async def shutdown() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception as exc:
            log.warning("redis_client: close error: %s", exc)
        _redis = None
    log.info("redis_client: stopped")


def get_client() -> Optional[aioredis.Redis]:
    return _redis
