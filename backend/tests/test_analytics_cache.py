"""app/core/analytics_cache.py — stale-while-revalidate, single-flight, TTLs.

These are the semantics the Tech & AI endpoints now depend on for latency:

- A FRESH entry is served with no recomputation.
- A STALE entry is served *immediately* and refreshed in the background. This is
  the behaviour `tech_utilization.py` was missing entirely (every call site did
  `_cached, _ = await cache_get(...)`, dropping the flag), which meant one
  unlucky user per stale window paid the whole multi-second aggregation.
- Concurrent MISSES collapse onto one computation via a Redis SET NX lock, so
  the ten SWR calls a Tech page fires on mount don't each recompute.
- Every failure mode degrades to "compute it yourself" — a Redis outage, a lock
  holder that dies, or a background refresh that raises must never make a
  request fail, hang, or serve wrong data.
- A date range that ends before the aggregation worker's rescan window gets a
  much longer TTL, because those rows are no longer changing.

Redis is faked at the `RedisCache.get_client` seam rather than mocking
`cache_get`/`cache_set`, so the real key construction, serialisation and
SET NX semantics all execute.
"""
import asyncio
import time
from datetime import date, timedelta

import pytest

from app.core import analytics_cache as ac
from app.core import RedisCache as rc


# ── A minimal in-memory stand-in for the bits of redis.asyncio we use ─────────

class FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[bytes, float]] = {}
        self.set_calls = 0

    def _live(self, k):
        hit = self.store.get(k)
        if hit is None:
            return None
        val, expires = hit
        if expires is not None and expires < time.time():
            del self.store[k]
            return None
        return val

    async def get(self, k):
        return self._live(k)

    async def setex(self, k, ttl, payload):
        self.store[k] = (payload, time.time() + ttl)

    async def set(self, k, v, nx=False, ex=None):
        self.set_calls += 1
        if nx and self._live(k) is not None:
            return None
        self.store[k] = (v, time.time() + ex if ex else None)
        return True

    async def delete(self, k):
        self.store.pop(k, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rc, "get_client", lambda: r)
    ac._bg_keys.clear()
    yield r
    ac._bg_keys.clear()


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(rc, "get_client", lambda: None)
    ac._bg_keys.clear()
    yield
    ac._bg_keys.clear()


def _counting_compute(value, calls):
    async def compute(_db):
        calls.append(1)
        return value
    return compute


# ── Fresh / stale / miss ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fresh_entry_is_served_without_recomputing(fake_redis):
    key, calls = ("t", "fresh"), []
    first = await ac.cached_or_compute(key, _counting_compute({"n": 1}, calls), None,
                                       fresh=60, stale=600)
    assert first == {"n": 1} and len(calls) == 1

    second = await ac.cached_or_compute(key, _counting_compute({"n": 2}, calls), None,
                                        fresh=60, stale=600)
    assert second == {"n": 1}, "a fresh entry must be served verbatim"
    assert len(calls) == 1, "a fresh entry must not recompute"


@pytest.mark.asyncio
async def test_stale_entry_is_served_immediately_and_refreshed_in_background(fake_redis, monkeypatch):
    key = ("t", "stale")
    await rc.cache_set(key, {"n": "old"}, fresh_secs=0, stale_secs=600)  # already stale

    refreshed = asyncio.Event()

    async def compute(_db):
        refreshed.set()
        return {"n": "new"}

    # The background path opens its own session; make that a no-op.
    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    import app.db.base as base
    monkeypatch.setattr(base, "AsyncSessionLocal", lambda: _NullSession())

    served = await ac.cached_or_compute(key, compute, None, fresh=0, stale=600)
    assert served == {"n": "old"}, "stale data must be served immediately, not awaited"

    await asyncio.wait_for(refreshed.wait(), timeout=2)
    for _ in range(50):
        await asyncio.sleep(0.02)
        if (await rc.cache_get(key))[0] == {"n": "new"}:
            break
    assert (await rc.cache_get(key))[0] == {"n": "new"}, "background refresh must land in the cache"


@pytest.mark.asyncio
async def test_burst_of_stale_reads_schedules_exactly_one_refresh(fake_redis, monkeypatch):
    key = ("t", "burst")
    await rc.cache_set(key, {"n": 0}, fresh_secs=0, stale_secs=600)
    calls = []

    async def compute(_db):
        calls.append(1)
        await asyncio.sleep(0.05)
        return {"n": 1}

    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    import app.db.base as base
    monkeypatch.setattr(base, "AsyncSessionLocal", lambda: _NullSession())

    await asyncio.gather(*[
        ac.cached_or_compute(key, compute, None, fresh=0, stale=600) for _ in range(10)
    ])
    await asyncio.sleep(0.2)
    assert len(calls) == 1, f"10 stale reads scheduled {len(calls)} refreshes, expected 1"


# ── Single-flight ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_misses_compute_once(fake_redis):
    key, calls = ("t", "single"), []

    async def compute(_db):
        calls.append(1)
        await asyncio.sleep(0.1)          # long enough for losers to queue behind it
        return {"n": len(calls)}

    results = await asyncio.gather(*[
        ac.cached_or_compute(key, compute, None, fresh=60, stale=600) for _ in range(8)
    ])
    assert len(calls) == 1, f"8 concurrent misses computed {len(calls)} times, expected 1"
    assert all(r == {"n": 1} for r in results), "losers must receive the winner's value"


@pytest.mark.asyncio
async def test_single_flight_disabled_lets_everyone_compute(fake_redis):
    key, calls = ("t", "nosingle"), []

    async def compute(_db):
        calls.append(1)
        await asyncio.sleep(0.05)
        return {"n": 1}

    await asyncio.gather(*[
        ac.cached_or_compute(key, compute, None, fresh=60, stale=600, single_flight=False)
        for _ in range(4)
    ])
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_slow_lock_winner_does_not_hang_the_losers(fake_redis, monkeypatch):
    """If the winner outlives LOCK_WAIT_SECS the losers must compute, not hang."""
    monkeypatch.setattr(rc, "LOCK_WAIT_SECS", 0.1)
    key = ("t", "slowwinner")
    await rc.try_acquire(key)             # someone else already holds it, and never finishes
    calls = []

    result = await ac.cached_or_compute(key, _counting_compute({"n": 9}, calls), None,
                                        fresh=60, stale=600)
    assert result == {"n": 9}
    assert len(calls) == 1, "a loser that times out must fall through and compute"


# ── Degradation ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_redis_down_still_serves_by_computing(no_redis):
    key, calls = ("t", "down"), []
    for _ in range(3):
        got = await ac.cached_or_compute(key, _counting_compute({"n": 1}, calls), None)
        assert got == {"n": 1}
    assert len(calls) == 3, "with no cache every call computes — but every call succeeds"


@pytest.mark.asyncio
async def test_try_acquire_is_permissive_when_redis_is_down(no_redis):
    assert await rc.try_acquire(("t", "nolock")) is True, \
        "with no lock service the safe default is 'everyone computes', never 'nobody does'"


@pytest.mark.asyncio
async def test_failing_background_refresh_never_breaks_the_request(fake_redis, monkeypatch):
    key = ("t", "bgboom")
    await rc.cache_set(key, {"n": "old"}, fresh_secs=0, stale_secs=600)

    async def boom(_db):
        raise RuntimeError("refresh exploded")

    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False
    import app.db.base as base
    monkeypatch.setattr(base, "AsyncSessionLocal", lambda: _NullSession())

    served = await ac.cached_or_compute(key, boom, None, fresh=0, stale=600)
    assert served == {"n": "old"}
    await asyncio.sleep(0.15)
    assert (await rc.cache_get(key))[0] == {"n": "old"}, "a failed refresh leaves the entry intact"
    assert key not in ac._bg_keys, "a failed refresh must release its in-flight guard"


@pytest.mark.asyncio
async def test_none_result_is_not_cached(fake_redis):
    """A compute that yields nothing must not poison the key with a null."""
    key = ("t", "none")

    async def compute(_db):
        return None

    assert await ac.cached_or_compute(key, compute, None) is None
    assert (await rc.cache_get(key))[0] is None


# ── Settled-window TTLs ───────────────────────────────────────────────────────

def test_recent_range_uses_short_ttl():
    fresh, stale = ac.ttl_for_range(date.today())
    assert (fresh, stale) == (rc.FRESH_ANALYTICS, rc.STALE_ANALYTICS)


def test_settled_range_uses_long_ttl():
    from app.core import tech_cache
    settled = date.today() - timedelta(days=tech_cache.RECENT_DAYS + 30)
    fresh, stale = ac.ttl_for_range(settled)
    assert (fresh, stale) == (ac.SETTLED_FRESH, ac.SETTLED_STALE)
    assert stale > rc.STALE_ANALYTICS


def test_ttl_boundary_follows_tech_cache_rescan_window():
    """The threshold must track tech_cache.RECENT_DAYS, not a hardcoded number —
    that is the window the aggregation worker itself still rescans."""
    from app.core import tech_cache
    inside  = date.today() - timedelta(days=tech_cache.RECENT_DAYS)
    outside = date.today() - timedelta(days=tech_cache.RECENT_DAYS + 1)
    assert ac.ttl_for_range(inside)  == (rc.FRESH_ANALYTICS, rc.STALE_ANALYTICS)
    assert ac.ttl_for_range(outside) == (ac.SETTLED_FRESH, ac.SETTLED_STALE)
