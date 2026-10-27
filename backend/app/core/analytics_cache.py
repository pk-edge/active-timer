"""Stale-while-revalidate + single-flight wrapper for the analytics endpoints.

Why this exists
───────────────
`RedisCache.cache_get` already returns `(data, is_stale)` and the module
docstring promises stale-while-revalidate semantics, but every call site in
`tech_utilization.py` was written as `_cached, _ = await cache_get(key)` —
throwing the flag away. The practical effect: an entry stayed useful for
STALE_ANALYTICS (10 min) and was then recomputed *synchronously* by whichever
user happened to ask first, who ate the full multi-second aggregation. Nothing
ever refreshed in the background.

`home.py` already solved this with a `_resp_bg_keys` guard; this module is that
pattern factored out so the eleven tech endpoints can share it, with the
addition of a cross-worker single-flight lock.

Behaviour
─────────
    fresh  → serve, do nothing
    stale  → serve immediately, refresh in a background task
    miss   → try to win the single-flight lock:
                won  → compute, store, serve
                lost → wait briefly for the winner's value, else compute anyway

Every failure mode degrades to "compute it yourself". A Redis outage, a crashed
lock holder, or a background task that raises must never make a request fail,
hang, or serve wrong data.

The `compute` callable takes an `AsyncSession` and returns a JSON-serialisable
value. It is called with the request's session on the hot path and with a fresh
`AsyncSessionLocal` on the background path — it must therefore not close over a
session of its own.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from app.core.RedisCache import (
    cache_get, cache_set, try_acquire, release, wait_for,
    FRESH_ANALYTICS, STALE_ANALYTICS,
)

log = logging.getLogger(__name__)

# Keys with a background refresh already in flight, so a burst of stale reads
# schedules exactly one. Process-local by design: the Redis lock handles the
# cross-worker case, and this avoids a Redis round-trip on the common path.
_bg_keys: set[tuple] = set()

# Strong refs to background tasks — asyncio only holds weak ones, so without
# this a refresh can be garbage-collected mid-flight.
_bg_tasks: set[asyncio.Task] = set()

Compute = Callable[[Any], Awaitable[Any]]


async def _refresh(key: tuple, compute: Compute, fresh: int, stale: int) -> None:
    from app.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            value = await compute(db)
        if value is not None:
            await cache_set(key, value, fresh_secs=fresh, stale_secs=stale)
    except Exception:
        # A failed refresh just means the entry stays stale and someone
        # retries later — never surface it to the request that triggered it.
        log.exception("analytics_cache: background refresh failed for %s", key[:3])
    finally:
        _bg_keys.discard(key)


def _schedule(key: tuple, compute: Compute, fresh: int, stale: int) -> None:
    if key in _bg_keys:
        return
    _bg_keys.add(key)
    task = asyncio.create_task(_refresh(key, compute, fresh, stale))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def cached_or_compute(
    key: tuple,
    compute: Compute,
    db: Any,
    *,
    fresh: int = FRESH_ANALYTICS,
    stale: int = STALE_ANALYTICS,
    single_flight: bool = True,
) -> Any:
    """Serve `key` from Redis, refreshing in the background when stale."""
    cached, is_stale = await cache_get(key)
    if cached is not None:
        if is_stale:
            _schedule(key, compute, fresh, stale)
        return cached

    # True miss. Collapse concurrent misses onto one computation where we can.
    if single_flight:
        token = await try_acquire(key)
        if token:
            try:
                value = await compute(db)
                if value is not None:
                    await cache_set(key, value, fresh_secs=fresh, stale_secs=stale)
                return value
            finally:
                await release(key, token)
        winner = await wait_for(key)
        if winner is not None:
            return winner
        # Winner was too slow or died — fall through and compute ourselves
        # rather than leaving this request hanging on someone else's work.

    value = await compute(db)
    if value is not None:
        await cache_set(key, value, fresh_secs=fresh, stale_secs=stale)
    return value


# ── Settled-window TTLs ───────────────────────────────────────────────────────
# A date range that ends before the cache worker's rescan window is one whose
# underlying rows are no longer changing in practice, so holding it for ten
# minutes is needlessly pessimistic — re-deriving "what did last month look
# like" every ten minutes is most of the date-filter cost.
#
# The threshold deliberately reuses tech_cache.RECENT_DAYS: that is the window
# the aggregation worker itself rescans each cycle, so anything older is already
# treated as settled by the layer below. Agents CAN still backfill older days
# (tech_cache sweeps BACKFILL_DAYS once a day for exactly that reason), which is
# why a settled window still expires within hours rather than being pinned.

SETTLED_FRESH = 30 * 60      # 30 min
SETTLED_STALE = 6 * 60 * 60  # 6 h


def ttl_for_range(until_dt) -> tuple[int, int]:
    """(fresh_secs, stale_secs) for a query ending on `until_dt`."""
    from datetime import date as _date, timedelta as _td
    from app.core import tech_cache

    if until_dt < _date.today() - _td(days=tech_cache.RECENT_DAYS):
        return SETTLED_FRESH, SETTLED_STALE
    return FRESH_ANALYTICS, STALE_ANALYTICS
