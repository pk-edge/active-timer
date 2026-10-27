"""Leader election for singleton background loops (multi-worker safe).

Why
───
With `uvicorn --workers N` every worker runs the whole lifespan, so loops that
operate on SHARED state (report_cache, tech_cache, device_janitor — they scan/
aggregate the one database) would run N times. They must run in exactly one
worker. The audit writer and ingest flusher drain PER-PROCESS buffers, so they
stay per-worker and are NOT gated.

Design — two layers, both must hold for a worker to lead
────────────────────────────────────────────────────────
1. Host file lock (fcntl.flock, non-blocking, exclusive). All workers of one
   container share a filesystem, so exactly one of them holds it; the KERNEL
   drops it the instant that process dies — no TTL, no stale leader, works
   with Redis down. Acquired FIRST.
2. Fleet Redis lock `tt:leader:<name>` = SET NX PX <ttl> with a random token,
   renewed every `interval` seconds by a token-checked Lua compare-and-pexpire
   and released by a token-checked compare-and-delete (a worker can never
   renew or delete a lock that expired and was taken by someone else).
   Only a worker that already holds the file lock competes for it, so within a
   container the Redis lock never splits leadership between workers.

   When Redis is unavailable (never started, errors, timeouts) the Redis layer
   is SKIPPED: the file-lock holder leads. That is exactly today's behaviour per
   replica (one leader per container), so it can never cause double work beyond
   what a single-worker-per-replica deployment already did. On a single host
   (the production target) it is exact. Across several replicas with Redis
   down, each replica's file-lock holder runs — identical to pre-election
   behaviour — and when Redis returns, all but one replica yield within one
   `interval`.

   If the lock file cannot be opened at all (read-only/odd filesystem) the host
   lock is NOT assumed held by everyone — that would elect every worker. The
   path falls back to the system temp dir; if even that fails the FileLock is
   `degraded` and a worker leads ONLY while it holds the Redis lock (so never
   with Redis down: no leader beats a leader per worker).

Failover: a SIGKILLed leader frees the file lock immediately, but its Redis key
lives until `ttl` expires (default 30 s), so takeover is ≤ ttl + interval.
Graceful shutdown releases both locks at once.

Leadership transitions call `on_gain()` / `on_lose()`; if on_gain raises the
worker is NOT marked leader and retries next tick. A lock that is merely
re-acquired after a transient Redis hiccup (key expired, nobody else took it)
is re-taken in the same tick with no on_lose/on_gain flap.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from typing import Awaitable, Callable, Optional

try:                                   # POSIX only; absent on Windows dev boxes
    import fcntl
except ImportError:                    # pragma: no cover
    fcntl = None                       # type: ignore[assignment]

log = logging.getLogger(__name__)

_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)

_REDIS_TIMEOUT = 2.0


class FileLock:
    """Non-blocking exclusive flock; released by the kernel if the process dies.

    `degraded` is True when no lock file could be opened anywhere: acquire()
    then returns True (so the elector proceeds to the Redis layer) but the host
    lock gives NO mutual exclusion and the caller must require the Redis lock.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None
        self.degraded = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _open(self) -> Optional[int]:
        candidates = [self.path]
        fallback = os.path.join(tempfile.gettempdir(), os.path.basename(self.path))
        if fallback != self.path:
            candidates.append(fallback)
        for cand in candidates:
            try:
                fd = os.open(cand, os.O_RDWR | os.O_CREAT, 0o644)
            except OSError as exc:
                log.warning("leader: cannot open lock file %s (%s)", cand, exc)
                continue
            if cand != self.path:
                log.warning("leader: using fallback lock file %s", cand)
                self.path = cand
            return fd
        return None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        if fcntl is None:
            self._fd = -1              # no flock available: treat as held (single-process dev)
            return True
        fd = self._open()
        if fd is None:
            if not self.degraded:
                log.warning("leader: no usable lock file — host lock disabled; this worker "
                            "will lead only while it holds the Redis lock")
            self.degraded = True
            self._fd = -1
            return True
        self.degraded = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None and fd >= 0:
            try:
                os.close(fd)           # closing the fd drops the flock
            except OSError:
                pass


class LeaderElector:
    def __init__(
        self,
        name: str,
        on_gain: Callable[[], Awaitable[None]],
        on_lose: Callable[[], Awaitable[None]],
        *,
        ttl_secs: float = 30.0,
        interval_secs: float = 10.0,
        lock_path: str | None = None,
        redis_getter: Callable[[], object] | None = None,
    ):
        self.name = name
        self._on_gain = on_gain
        self._on_lose = on_lose
        self._ttl_ms = int(ttl_secs * 1000)
        self._interval = interval_secs
        self._file = FileLock(lock_path or os.path.join(
            tempfile.gettempdir(), f"tt_leader_{name}.lock"))
        self._redis_getter = redis_getter or self._default_redis
        self._key = f"tt:leader:{name}"
        self._token = uuid.uuid4().hex          # unique per process/elector instance
        self._holds_redis = False
        self._leader = False
        self._task: asyncio.Task | None = None

    @staticmethod
    def _default_redis():
        from app.core.RedisClient import get_client
        return get_client()

    @property
    def is_leader(self) -> bool:
        return self._leader

    # ── Redis primitives (token-checked) ─────────────────────────────────────
    async def _redis_acquire(self, r) -> bool:
        ok = await asyncio.wait_for(
            r.set(self._key, self._token, nx=True, px=self._ttl_ms), _REDIS_TIMEOUT)
        return bool(ok)

    async def _redis_renew(self, r) -> bool:
        res = await asyncio.wait_for(
            r.eval(_RENEW_LUA, 1, self._key, self._token, self._ttl_ms), _REDIS_TIMEOUT)
        return bool(res)

    async def _redis_release(self, r) -> None:
        try:
            await asyncio.wait_for(
                r.eval(_RELEASE_LUA, 1, self._key, self._token), _REDIS_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — TTL will free it anyway
            log.debug("leader[%s]: redis release failed: %s", self.name, exc)

    # ── One election step ────────────────────────────────────────────────────
    async def _should_lead(self) -> bool:
        if not self._file.acquire():
            return False                        # another worker on this host leads
        degraded = self._file.degraded          # no host lock available: Redis is mandatory
        r = self._redis_getter()
        if r is None:
            return not degraded                 # Redis not configured/started: host lock decides
        try:
            if self._holds_redis:
                if await self._redis_renew(r):
                    return True
                self._holds_redis = False       # lost it (expired / taken) — try to retake
            if await self._redis_acquire(r):
                self._holds_redis = True
                return True
            return False                        # another replica holds the fleet lock
        except Exception as exc:  # noqa: BLE001 — Redis down/slow: host lock decides
            if degraded:
                log.warning("leader[%s]: redis unavailable (%s) and no host lock — not leading",
                            self.name, type(exc).__name__)
                return False
            log.warning("leader[%s]: redis unavailable (%s) — host lock decides",
                        self.name, type(exc).__name__)
            return True

    async def tick(self) -> bool:
        want = await self._should_lead()
        if want and not self._leader:
            try:
                await self._on_gain()
            except Exception:  # noqa: BLE001
                log.exception("leader[%s]: on_gain failed — will retry", self.name)
                return self._leader
            self._leader = True
            log.info("leader[%s]: LEADER (pid=%d) — starting singleton loops", self.name, os.getpid())
        elif not want and self._leader:
            await self._demote("lost leadership")
        return self._leader

    async def _demote(self, why: str) -> None:
        self._leader = False
        log.info("leader[%s]: not leader (%s) — stopping singleton loops", self.name, why)
        try:
            await self._on_lose()
        except Exception:  # noqa: BLE001
            log.exception("leader[%s]: on_lose failed", self.name)

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None:
            return
        try:
            await self.tick()                   # decide before serving, so logs show it at boot
        except Exception:  # noqa: BLE001
            log.exception("leader[%s]: initial election failed", self.name)
        self._task = asyncio.create_task(self._loop(), name=f"leader-{self.name}")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("leader[%s]: election tick failed", self.name)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._leader:
            await self._demote("shutdown")
        if self._holds_redis:
            r = self._redis_getter()
            if r is not None:
                await self._redis_release(r)
            self._holds_redis = False
        self._file.release()
