"""In-process admin-auth cache — takes the per-request user+permission SELECT off the DB.

Why
───
`get_current_user` runs a joined query (employee → role → permissions) on EVERY
authenticated dashboard request. A single page load fires several parallel API
calls with the same bearer token, so the same row set is re-read many times per
second per user. The result is already returned as a DETACHED, read-only ORM
instance (see the expunge design in app/api/deps.py) — which makes it safe to
hand the same detached object to subsequent requests for a short window.

Design
──────
• Pure in-process dict (like device_auth_cache) — zero Redis/DB on a warm hit.
• TTL is deliberately SHORT (15s): the backstop if an invalidation message is
  missed (Redis down).
• invalidate_all() is called by every user/role mutation endpoint: INSTANT on
  the worker that served the mutation (this is also what keeps single-process
  test suites exact), and it fires `on_invalidate` — wired by
  app/core/AuthInvalidationBus.py to a Redis pub/sub broadcast — so sibling
  workers/replicas drop theirs immediately too. Receivers call
  invalidate_local(), which never republishes.
• JWT signature + expiry are still verified on every request BEFORE the cache
  is consulted — the cache only skips the DB re-read, never token validation.
• Epoch guard closes the mutate-during-lookup race exactly like
  device_auth_cache: a set() racing an invalidation is dropped.
"""

from __future__ import annotations

import time

MAX_ENTRIES = 5_000


class AdminAuthCache:
    def __init__(self, ttl_secs: float, max_entries: int = MAX_ENTRIES,
                 negative_ttl_secs: float = 0.0, negative_max: int = 5_000) -> None:
        self._ttl = ttl_secs
        self._max = max_entries
        # token -> expiry. A validly-SIGNED token whose employee is missing or
        # inactive: remembered briefly so a stale/deactivated session retrying
        # doesn't cost a joined SELECT per request. Cleared by invalidate_all()
        # (every user/role mutation), so (re)activation/approval is instant on
        # the mutating worker. Follows the main TTL switch (ttl<=0 disables).
        self._neg_ttl = negative_ttl_secs
        self._neg_max = negative_max
        self._negatives: dict[str, float] = {}
        # token -> (detached Employee with _perms/_scope_type attached, expiry)
        self._entries: dict[str, tuple[object, float]] = {}
        self._epoch = 0
        # Sync hook fired after a LOCAL invalidate_all() (cross-worker broadcast).
        # Must never raise into the mutating endpoint — guarded below anyway.
        self.on_invalidate = None

    def get(self, token: str):
        if self._ttl <= 0:          # disabled (e.g. tests set ADMIN_AUTH_CACHE_TTL_SECS=0)
            return None
        hit = self._entries.get(token)
        if hit is None:
            return None
        user, expiry = hit
        if expiry <= time.monotonic():
            self._entries.pop(token, None)
            return None
        return user

    def epoch(self) -> int:
        return self._epoch

    def set(self, token: str, user: object, seen_epoch: int | None = None) -> None:
        if seen_epoch is not None and seen_epoch != self._epoch:
            return
        if len(self._entries) >= self._max and token not in self._entries:
            overflow = len(self._entries) - self._max + 1
            for old in list(self._entries.keys())[:overflow]:
                self._entries.pop(old, None)
        self._entries[token] = (user, time.monotonic() + self._ttl)

    def is_negative(self, token: str) -> bool:
        if self._ttl <= 0 or self._neg_ttl <= 0:
            return False
        exp = self._negatives.get(token)
        if exp is None:
            return False
        if exp <= time.monotonic():
            self._negatives.pop(token, None)
            return False
        return True

    def set_negative(self, token: str, seen_epoch: int | None = None) -> None:
        if self._ttl <= 0 or self._neg_ttl <= 0:
            return
        if seen_epoch is not None and seen_epoch != self._epoch:
            return
        if len(self._negatives) >= self._neg_max and token not in self._negatives:
            overflow = len(self._negatives) - self._neg_max + 1
            for old in list(self._negatives.keys())[:overflow]:
                self._negatives.pop(old, None)
        self._negatives[token] = time.monotonic() + self._neg_ttl

    def invalidate_local(self) -> None:
        """Drop this process's entries only (no broadcast) — also what an
        incoming cross-worker invalidation applies."""
        self._epoch += 1
        self._entries.clear()
        self._negatives.clear()

    def invalidate_all(self) -> None:
        """Blunt but correct: user/role mutations are rare and re-warming is one
        indexed query per active dashboard user, so per-user granularity isn't
        worth the bookkeeping. Clears locally, then broadcasts to sibling workers."""
        self.invalidate_local()
        hook = self.on_invalidate
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 — the mutation must not fail on a broadcast
                pass


# Singleton, configured from settings at import time.
from app.core.config import get_settings  # noqa: E402

admin_auth_cache = AdminAuthCache(
    ttl_secs=get_settings().ADMIN_AUTH_CACHE_TTL_SECS,
    negative_ttl_secs=get_settings().ADMIN_AUTH_NEGATIVE_TTL_SECS,
    negative_max=get_settings().ADMIN_AUTH_NEGATIVE_MAX,
)
