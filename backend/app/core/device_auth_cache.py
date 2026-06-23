"""In-process device-auth cache — takes the per-POST ingest auth SELECT off the DB.

Why
───
Every agent POSTs /v1/ingest ~every 5s. Validating the device JWT used to run a
SELECT on `devices` for EVERY post, each one borrowing a connection from the
shared 10-slot HTTP pool. At 1500 devices that is ~300 auth SELECTs/s competing
with dashboard traffic — the exact contention that overflows the pool under
bursts. Device tokens are long-lived (365d) and rarely change, so the
(token → device_id) mapping is highly cacheable.

This cache makes the steady-state ingest auth path touch the DB ZERO times: a
warm token resolves from memory; only a cold/expired token does one short DB
lookup (and that lookup is concurrency-bounded by the caller so it can never
exhaust a pool).

Design
──────
• Pure in-process dict — NO Redis dependency, so there is no Redis error path
  when Redis is absent. (Cross-replica sharing can be layered on later via the
  existing broadcaster; each replica simply keeps its own warm cache today.)
• TTL bounds staleness: a revoked/archived device keeps validating for at most
  TTL seconds unless explicitly invalidated (archive/delete/re-enroll call
  invalidate_device()). TTL is deliberately short (default 60s).
• Bounded size (LRU-ish by insertion order) so memory can never grow unbounded.
• Single-event-loop process (uvicorn --workers 1): all methods are synchronous
  with no awaits, so each call is atomic w.r.t. the asyncio scheduler — no locks
  needed.
"""

from __future__ import annotations

import time


class DeviceAuthCache:
    def __init__(self, ttl_secs: float, max_entries: int) -> None:
        self._ttl = ttl_secs
        self._max = max_entries
        # token -> (device_id, expiry_monotonic)
        self._entries: dict[str, tuple[str, float]] = {}
        # device_id -> set of tokens (for O(1) invalidation on revoke)
        self._device_tokens: dict[str, set[str]] = {}
        # Monotonic invalidation counter. A cold auth lookup captures this BEFORE
        # its DB read; set() refuses to cache if it advanced meanwhile — closing
        # the TOCTOU window where an admin revoke fires while the token is still
        # in-flight (not yet cached, so invalidate_device finds nothing to drop).
        self._epoch = 0

    def get(self, token: str) -> str | None:
        """Return the cached device_id for a token, or None on miss/expiry."""
        hit = self._entries.get(token)
        if hit is None:
            return None
        device_id, expiry = hit
        if expiry <= time.monotonic():
            self._evict_token(token)
            return None
        return device_id

    def epoch(self) -> int:
        """Current invalidation epoch — capture before a cold lookup, pass to set()."""
        return self._epoch

    def set(self, token: str, device_id: str, seen_epoch: int | None = None) -> None:
        """Cache a validated (token → device_id) mapping for TTL seconds.

        If `seen_epoch` is given and an invalidation happened since it was
        captured (i.e. the epoch advanced), the insert is SKIPPED — a revoke
        raced this in-flight lookup, so we must not re-warm the cache. This and
        set() being synchronous (no await) means a concurrent invalidate either
        bumps the epoch before this check (→ skip) or runs after the insert
        completes (→ it finds and drops the freshly-added token). Either way a
        revoked device cannot linger in the cache.
        """
        if seen_epoch is not None and seen_epoch != self._epoch:
            return
        # Evict oldest entries first if we are at capacity (dict preserves
        # insertion order, so the first keys are the oldest inserted).
        if len(self._entries) >= self._max and token not in self._entries:
            overflow = len(self._entries) - self._max + 1
            for old in list(self._entries.keys())[:overflow]:
                self._evict_token(old)
        self._entries[token] = (device_id, time.monotonic() + self._ttl)
        self._device_tokens.setdefault(device_id, set()).add(token)

    def invalidate_device(self, device_id: str) -> None:
        """Drop every cached token for a device — call on archive/delete/revoke."""
        self._epoch += 1
        for token in self._device_tokens.pop(device_id, set()):
            self._entries.pop(token, None)

    def _evict_token(self, token: str) -> None:
        entry = self._entries.pop(token, None)
        if entry is not None:
            device_id = entry[0]
            toks = self._device_tokens.get(device_id)
            if toks is not None:
                toks.discard(token)
                if not toks:
                    self._device_tokens.pop(device_id, None)

    def clear(self) -> None:
        self._entries.clear()
        self._device_tokens.clear()

    def stats(self) -> dict:
        return {"tokens": len(self._entries), "devices": len(self._device_tokens)}


# Singleton, configured from settings at import time.
from app.core.config import get_settings  # noqa: E402

_settings = get_settings()
device_auth_cache = DeviceAuthCache(
    ttl_secs=_settings.INGEST_AUTH_CACHE_TTL_SECS,
    max_entries=_settings.INGEST_AUTH_CACHE_MAX,
)
