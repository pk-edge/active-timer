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
