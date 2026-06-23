import asyncio
import logging
from typing import Optional, Set

import redis.asyncio as aioredis  # type: ignore  # resolved in the Docker image (requirements.txt: redis==5.2.1); the local IDE interpreter has no backend deps installed

from app.core.config import get_settings

log = logging.getLogger(__name__)

# Channel every backend process publishes to and subscribes from.
CHANNEL = "devices_updated"

# Per-process set of live SSE subscriber queues. Mutated only on the event loop.
_subscribers: Set[asyncio.Queue] = set()

# Shared client (its pool serves publish) + the background listener task.
_redis: Optional[aioredis.Redis] = None
_listener_task: Optional[asyncio.Task] = None
_stopping: bool = False


# ── Lifespan: wired into app.main.lifespan ────────────────────────────────────

async def startup() -> None:
    """Open the shared Redis client and start the background listener.

    Idempotent. If Redis is not reachable at startup, SSE push is disabled for the
    lifetime of this process — no reconnect loop, no log spam, and publish() returns
    immediately (the _redis is None guard). This prevents publish() from blocking the
    ingest write path for up to 5 s on every call while waiting for a connect timeout.
    """
    global _redis, _listener_task, _stopping
    if _listener_task is not None:
        return  # already started

    _stopping = False
    settings = get_settings()

    # One-shot probe: check reachability BEFORE creating the shared client.
    # If Redis is absent (no container, no env var, wrong host) we log once and
    # return — _redis stays None, publish() is a cheap no-op, no task is spawned.
    probe = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
    redis_available = False
    try:
        await asyncio.wait_for(probe.ping(), timeout=3.0)
        redis_available = True
    except Exception as exc:
        log.warning("broadcaster: Redis not reachable (%s) — SSE push disabled", exc)
    finally:
        try:
            await probe.aclose()
        except Exception:
            pass

    if not redis_available:
        return  # _redis stays None; no listener task; no reconnect attempts

    # health_check_interval pings idle connections so a silently-dropped TCP
    # connection is detected and recycled rather than hanging a publish/listen.
    _redis = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,          # message["data"] arrives as str
        health_check_interval=30,
        socket_keepalive=True,
        socket_connect_timeout=5,
    )
    _listener_task = asyncio.create_task(_run_listener(), name="sse-broadcaster-listener")
    log.info("broadcaster: started (channel=%r)", CHANNEL)


async def shutdown() -> None:
    """Stop the listener and close the Redis client cleanly."""
    global _redis, _listener_task, _stopping
    _stopping = True

    if _listener_task is not None:
        _listener_task.cancel()
        try:
            await _listener_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("broadcaster: listener shutdown error: %s", exc)
        _listener_task = None

    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("broadcaster: redis close error: %s", exc)
        _redis = None

    log.info("broadcaster: stopped")


# ── Background listener ───────────────────────────────────────────────────────

async def _run_listener() -> None:
    """Subscribe to Redis and fan messages out to local SSE queues forever.

    Reconnects with capped exponential backoff so a Redis restart or network blip
    degrades live push for a few seconds instead of disabling it for the life of
    the process.
    """
    backoff = 1.0
    while not _stopping:
        pubsub = None
        try:
            assert _redis is not None
            pubsub = _redis.pubsub()
            await pubsub.subscribe(CHANNEL)
            backoff = 1.0  # reset after a successful (re)subscribe
            log.info("broadcaster: subscribed to Redis channel %r", CHANNEL)

            async for message in pubsub.listen():
                if _stopping:
                    break
                if message.get("type") != "message":
                    continue  # skip subscribe/unsubscribe confirmations
                _fanout(message["data"])

        except asyncio.CancelledError:
            raise  # shutdown() requested — propagate so the task ends
        except Exception as exc:
            log.warning(
                "broadcaster: Redis listener error (%s); reconnecting in %.0fs",
                exc, backoff,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, 30.0)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass  # best-effort; about to reconnect or shut down anyway


def _fanout(message: str) -> None:
    """Deliver a message to every local SSE subscriber (fire-and-forget)."""
    for q in list(_subscribers):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass  # slow subscriber — drop rather than block the listener


# ── Public API (unchanged signatures, except publish is now async) ────────────

def subscribe() -> asyncio.Queue:
    """Register a new SSE subscriber and return its queue. No cap."""
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


async def publish(message: str) -> None:
    """Broadcast a message to all connected SSE clients across all replicas.

    NEVER raises: it runs immediately after a committed DB write in the ingest
    path, so a Redis hiccup must not turn a successful write into a flush error.
    """
    if _redis is None:
        log.debug("broadcaster: publish skipped — Redis not started")
        return
    try:
        await _redis.publish(CHANNEL, message)
    except Exception as exc:
        log.warning("broadcaster: publish failed (%s) — event dropped", exc)


def subscriber_count() -> int:
    """Local (this-process) SSE subscriber count — useful for metrics/debugging."""
    return len(_subscribers)
