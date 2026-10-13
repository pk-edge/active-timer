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
• One event loop per worker process: all methods are synchronous with no
  awaits, so each call is atomic w.r.t. the asyncio scheduler — no locks needed.
  With several workers each keeps its own cache; the async module-level
  invalidate_device() below clears THIS worker's cache and broadcasts the
  device_id over Redis pub/sub (app/core/AuthInvalidationBus.py) so every other
  worker/replica drops it too. If a message is missed (Redis down) the TTL
  (60s positive / negative) is the backstop.
• Negative entries (is_negative/set_negative): a validly-signed token whose
  device is missing/revoked is remembered briefly so a revoked agent retrying
  every 5 s costs ZERO DB reads. invalidate_device() drops them, and set_negative
  honours the epoch guard, so an enrolled/restored device is never locked out.
"""

from __future__ import annotations

import time


class DeviceAuthCache:
    def __init__(self, ttl_secs: float, max_entries: int,
                 negative_ttl_secs: float = 0.0, negative_max: int = 10_000) -> None:
        self._ttl = ttl_secs
        self._max = max_entries
        # Negative entries: validly-SIGNED tokens whose device is missing/revoked/
        # rotated-out. token -> (device_id, expiry_monotonic); reverse index by
        # device so enroll/restore/archive/delete can drop them. Only tokens that
        # pass JWT verification ever get here (callers verify first), so the set
        # is bounded by minted-then-revoked tokens, and capped by `negative_max`.
        self._neg_ttl = negative_ttl_secs
        self._neg_max = negative_max
        self._negatives: dict[str, tuple[str, float]] = {}
        self._device_negatives: dict[str, set[str]] = {}
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

    # ── Negative entries ─────────────────────────────────────────────────────
    def is_negative(self, token: str) -> bool:
        """True if this (validly signed) token was recently found invalid/revoked."""
        if self._neg_ttl <= 0:
            return False
        hit = self._negatives.get(token)
        if hit is None:
            return False
        if hit[1] <= time.monotonic():
            self._evict_negative(token)
            return False
        return True

    def set_negative(self, token: str, device_id: str, seen_epoch: int | None = None) -> None:
        """Remember that `token` failed DB validation, for negative_ttl seconds.

        Skipped when an invalidation happened since `seen_epoch` was captured —
        an enroll/restore racing the in-flight lookup must never be shadowed by
        a stale "revoked" verdict (same TOCTOU guard as set()).
        """
        if self._neg_ttl <= 0:
            return
        if seen_epoch is not None and seen_epoch != self._epoch:
            return
        if len(self._negatives) >= self._neg_max and token not in self._negatives:
            overflow = len(self._negatives) - self._neg_max + 1
            for old in list(self._negatives.keys())[:overflow]:
                self._evict_negative(old)
        self._negatives[token] = (device_id, time.monotonic() + self._neg_ttl)
        self._device_negatives.setdefault(device_id, set()).add(token)

    def _evict_negative(self, token: str) -> None:
        entry = self._negatives.pop(token, None)
        if entry is not None:
            toks = self._device_negatives.get(entry[0])
            if toks is not None:
                toks.discard(token)
                if not toks:
                    self._device_negatives.pop(entry[0], None)

    def invalidate_device(self, device_id: str) -> None:
        """Drop every cached token (positive AND negative) for a device — call on
        enroll / restore / archive / delete / revoke."""
        self._epoch += 1
        for token in self._device_tokens.pop(device_id, set()):
            self._entries.pop(token, None)
        for token in self._device_negatives.pop(device_id, set()):
            self._negatives.pop(token, None)

    def invalidate_all(self) -> None:
        """Drop everything and advance the epoch (used after a pub/sub gap)."""
        self._epoch += 1
        self.clear()

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
        self._negatives.clear()
        self._device_negatives.clear()

    def stats(self) -> dict:
        return {"tokens": len(self._entries), "devices": len(self._device_tokens),
                "negatives": len(self._negatives)}


# Singleton, configured from settings at import time.
from app.core.config import get_settings  # noqa: E402

_settings = get_settings()
device_auth_cache = DeviceAuthCache(
    ttl_secs=_settings.INGEST_AUTH_CACHE_TTL_SECS,
    max_entries=_settings.INGEST_AUTH_CACHE_MAX,
    negative_ttl_secs=_settings.INGEST_AUTH_NEGATIVE_TTL_SECS,
    negative_max=_settings.INGEST_AUTH_NEGATIVE_MAX,
)


def device_ctx_cache_key(device_id: str) -> tuple:
    """Redis key tuple for the shared device-context cache (see deps.get_device_ctx).

    Keyed by device_id (not token) so a mutation can evict it across ALL
    replicas with one DEL — an in-process ctx cache can't do that, and the
    agent-facing promise is 'settings reach the agent on its next ~60s poll'.
    """
    return ("devctx", device_id)


def device_ctx_tombstone_key(device_id: str) -> tuple:
    return ("devctx_tomb", device_id)


# How long a fresh invalidation blocks re-caching of the device ctx. Closes the
# race where an in-flight poll read the device row BEFORE the mutation committed
# and would otherwise cache_set the stale ctx AFTER the invalidation's DEL. Must
# comfortably exceed any realistic request duration.
CTX_TOMBSTONE_SECS = 30


async def invalidate_device(device_id: str) -> None:
    """Drop every cached auth/ctx entry for a device — the in-process ingest
    token cache on THIS replica plus the Redis device-context entry shared by
    all replicas. Call on archive / delete / re-enroll / reassign / headless
    changes so a stale context can never outlive the mutation by more than the
    in-process TTL (and, with Redis up, not even that). The tombstone is written FIRST so there is no window where
    the entry is gone but an in-flight lookup may re-warm it."""
    device_auth_cache.invalidate_device(device_id)
    # Tell the sibling workers/replicas (never raises; TTL is the backstop).
    from app.core.AuthInvalidationBus import auth_bus
    await auth_bus.publish_device(device_id)
    from app.core.RedisCache import cache_delete, cache_set
    await cache_set(device_ctx_tombstone_key(device_id), True,
                    fresh_secs=CTX_TOMBSTONE_SECS, stale_secs=CTX_TOMBSTONE_SECS)
    await cache_delete(device_ctx_cache_key(device_id))
