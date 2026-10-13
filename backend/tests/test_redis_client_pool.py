"""RedisClient uses a BLOCKING bounded pool (no instant 'Too many connections')."""
import asyncio

import pytest
import redis.asyncio as aioredis

from app.core import RedisClient


class _Probe:
    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_startup_builds_a_blocking_pool_with_configured_bounds(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(RedisClient.aioredis, "from_url", lambda *a, **k: _Probe())
    s = get_settings()
    monkeypatch.setattr(s, "REDIS_POOL_MAX_CONNECTIONS", 32)
    monkeypatch.setattr(s, "REDIS_POOL_TIMEOUT_SECS", 0.3)
    await RedisClient.startup()
    try:
        r = RedisClient.get_client()
        pool = r.connection_pool
        assert isinstance(pool, aioredis.BlockingConnectionPool)
        assert pool.max_connections == 32 and pool.timeout == 0.3
    finally:
        await RedisClient.shutdown()
    assert RedisClient.get_client() is None and RedisClient._pool is None


@pytest.mark.asyncio
async def test_blocking_pool_waits_for_a_free_connection_instead_of_failing_fast():
    """The failure mode we are fixing: a non-blocking pool raises immediately when
    all connections are in use; the blocking one hands the next free one over."""
    pool = aioredis.BlockingConnectionPool(max_connections=1, timeout=0.5)

    class C:
        def __init__(self, **kw): pass
        async def connect(self): pass
        async def disconnect(self, *a, **k): pass
        def can_read(self): return False
        async def can_read_destructive(self): return False
        is_connected = True
    pool.connection_class = C
    c1 = await pool.get_connection("GET")
    waiter = asyncio.create_task(pool.get_connection("GET"))
    await asyncio.sleep(0.05)
    assert not waiter.done()                           # waiting, not raised
    await pool.release(c1)
    assert (await asyncio.wait_for(waiter, 1)) is not None
    # and when nothing frees up it times out quickly (bounded) rather than hanging
    with pytest.raises(aioredis.ConnectionError):
        await pool.get_connection("GET")
