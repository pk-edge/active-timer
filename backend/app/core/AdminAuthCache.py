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
• TTL is deliberately SHORT (15s): a deactivated user or a permission change is
  live within 15s on other replicas even if an invalidation is missed.
• invalidate_all() is called by every user/role mutation endpoint, so on the
  replica that served the mutation the change is INSTANT (this is also what
  keeps single-process test suites exact).
• JWT signature + expiry are still verified on every request BEFORE the cache
  is consulted — the cache only skips the DB re-read, never token validation.
• Epoch guard closes the mutate-during-lookup race exactly like
  device_auth_cache: a set() racing an invalidation is dropped.
"""

from __future__ import annotations

import time

MAX_ENTRIES = 5_000


class AdminAuthCache:
    def __init__(self, ttl_secs: float, max_entries: int = MAX_ENTRIES) -> None:
        self._ttl = ttl_secs
        self._max = max_entries
        # token -> (detached Employee with _perms/_scope_type attached, expiry)
        self._entries: dict[str, tuple[object, float]] = {}
        self._epoch = 0

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

    def invalidate_all(self) -> None:
        """Blunt but correct: user/role mutations are rare and re-warming is one
        indexed query per active dashboard user, so per-user granularity isn't
        worth the bookkeeping."""
        self._epoch += 1
        self._entries.clear()


# Singleton, configured from settings at import time.
from app.core.config import get_settings  # noqa: E402

admin_auth_cache = AdminAuthCache(ttl_secs=get_settings().ADMIN_AUTH_CACHE_TTL_SECS)
