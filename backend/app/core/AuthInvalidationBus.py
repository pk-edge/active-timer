"""Cross-worker invalidation of the in-process auth caches (Redis pub/sub).

Why
───
`device_auth_cache` and `admin_auth_cache` are per-PROCESS dicts. With several
uvicorn workers (and replicas) a mutation only cleared the cache of the worker
that served it, so a revoked device kept ingesting for up to its 60 s TTL and a
deactivated admin / changed role kept its permissions for up to 15 s on the
sibling worker.

Design
──────
• One small dedicated channel (`tt:auth_invalidate`) — NOT the SSE
  `devices_updated` channel, whose payloads are fanned out to browsers.
• Message = JSON {"k": "device"|"admin", "id": <device_id>, "w": <sender id>}.
• Publish: `publish_device()` (awaited by device_auth_cache.invalidate_device)
  and `publish_admin_nowait()` (hooked on AdminAuthCache.invalidate_all, which
  is a sync call from many endpoints, so the publish is scheduled as a task).
  Publishing NEVER raises: the mutation has already been committed, a Redis
  hiccup only leaves the TTL as the backstop.
• Subscribe: every worker runs one listener on its OWN pub/sub connection
  (not the cache pool) and applies messages to its LOCAL caches only
  (`invalidate_device` / `invalidate_local`), which never republish — no loops.
  A worker skips its own messages (it already applied them synchronously);
  applying one anyway would be harmless (invalidations are idempotent).
• After a listener reconnect following a failure, both caches are cleared:
  any message published during the gap was lost.
• Redis unreachable at startup → no listener (same stance as the broadcaster);
  the TTLs bound staleness exactly as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

CHANNEL = "tt:auth_invalidate"
_PUBLISH_TIMEOUT = 1.0


class AuthInvalidationBus:
    def __init__(
        self,
        device_cache,
        admin_cache,
        *,
        redis_getter: Callable[[], object] | None = None,
        pubsub_factory: Callable[[], Awaitable[tuple[object, Callable[[], Awaitable[None]]]]] | None = None,
    ):
        self._device = device_cache
        self._admin = admin_cache
        self._redis_getter = redis_getter or self._default_redis
        self._pubsub_factory = pubsub_factory or self._default_pubsub
        self.worker_id = uuid.uuid4().hex
        self._task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False

    # ── defaults (production wiring) ─────────────────────────────────────────
    @staticmethod
    def _default_redis():
        from app.core.RedisClient import get_client
        return get_client()

    @staticmethod
    async def _default_pubsub():
        """-> (pubsub, close_coro): a dedicated connection, never the cache pool."""
        import redis.asyncio as aioredis
        from app.core.config import get_settings
        client = aioredis.from_url(
            get_settings().REDIS_URL, decode_responses=True,
            health_check_interval=30, socket_keepalive=True, socket_connect_timeout=5,
        )
        pubsub = client.pubsub()

        async def _close() -> None:
            try:
                await pubsub.aclose()
            finally:
                await client.aclose()
        return pubsub, _close

    # ── publish ──────────────────────────────────────────────────────────────
    async def _publish(self, payload: dict) -> None:
        r = self._redis_getter()
        if r is None:
            return
        payload["w"] = self.worker_id
        try:
            await asyncio.wait_for(
                r.publish(CHANNEL, json.dumps(payload, separators=(",", ":"))),
                _PUBLISH_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — never break the (committed) mutation
            log.warning("auth_bus: publish failed (%s) — siblings converge via TTL",
                        type(exc).__name__)

    async def publish_device(self, device_id: str) -> None:
        await self._publish({"k": "device", "id": str(device_id)})

    async def publish_admin(self) -> None:
        await self._publish({"k": "admin"})

    def publish_admin_nowait(self) -> None:
        """Sync entry point (AdminAuthCache.invalidate_all hook). No running loop
        (scripts/tests) → nothing to do, the local invalidation already happened."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        t = loop.create_task(self.publish_admin(), name="auth_bus_publish_admin")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ── receive ──────────────────────────────────────────────────────────────
    def handle(self, raw) -> bool:
        """Apply one message to the LOCAL caches. Returns True when applied.
        Never raises, never republishes."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode()
            msg = json.loads(raw)
            if msg.get("w") == self.worker_id:
                return False                    # own message: already applied synchronously
            kind = msg.get("k")
            if kind == "device" and msg.get("id"):
                self._device.invalidate_device(str(msg["id"]))
                return True
            if kind == "admin":
                self._admin.invalidate_local()
                return True
        except Exception:  # noqa: BLE001
            log.debug("auth_bus: ignoring malformed message", exc_info=True)
        return False

    def _flush_local(self) -> None:
        """Messages may have been lost during a listener gap: drop everything."""
        try:
            self._device.invalidate_all()
            self._admin.invalidate_local()
        except Exception:  # noqa: BLE001
            log.exception("auth_bus: local flush failed")

    # ── listener lifecycle ───────────────────────────────────────────────────
    async def _run_listener(self) -> None:
        backoff = 1.0
        had_failure = False
        while not self._stopping:
            close = None
            try:
                pubsub, close = await self._pubsub_factory()
                await pubsub.subscribe(CHANNEL)
                if had_failure:
                    self._flush_local()
                    had_failure = False
                backoff = 1.0
                log.info("auth_bus: subscribed to %r", CHANNEL)
                async for message in pubsub.listen():
                    if self._stopping:
                        break
                    if message.get("type") != "message":
                        continue
                    self.handle(message["data"])
                # listen() ended without error (connection dropped): resubscribe
                had_failure = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                had_failure = True
                log.warning("auth_bus: listener error (%s); reconnecting in %.0fs",
                            type(exc).__name__, backoff)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, 30.0)
            finally:
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass

    async def start(self) -> None:
        """Idempotent. Hooks the admin cache and starts the listener (only when
        Redis is up, like the broadcaster — otherwise TTLs bound staleness)."""
        self._admin.on_invalidate = self.publish_admin_nowait
        if self._task is not None:
            return
        if self._redis_getter() is None:
            log.info("auth_bus: Redis not available — cross-worker auth invalidation off")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_listener(), name="auth_invalidation_bus")

    async def stop(self) -> None:
        self._stopping = True
        if getattr(self._admin, "on_invalidate", None) == self.publish_admin_nowait:
            self._admin.on_invalidate = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            self._task = None
        for t in list(self._tasks):
            t.cancel()


# Singleton wired to the real caches; started/stopped from app.main.lifespan.
from app.core.AdminAuthCache import admin_auth_cache  # noqa: E402
from app.core.device_auth_cache import device_auth_cache  # noqa: E402

auth_bus = AuthInvalidationBus(device_auth_cache, admin_auth_cache)
