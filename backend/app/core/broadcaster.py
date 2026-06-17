"""
In-process pub/sub for SSE push notifications.

Each SSE subscriber gets its own asyncio.Queue. When the ingest endpoint
updates last_seen it calls publish() — every connected dashboard tab
receives the event and triggers an immediate SWR re-fetch.

Design constraints:
- All operations are called from the same asyncio event loop thread, so no
  locking is needed for the set itself (no await between set mutations).
- MAX_SUBSCRIBERS is a hard cap: subscribe() returns None when exceeded so
  endpoints can reject with 503 rather than silently accumulating queues.
- Queue maxsize=64: slow subscribers drop messages rather than blocking the
  publisher. The frontend always re-fetches current state on any event, so
  dropped intermediate events are harmless — only the absence of ALL events
  would matter, and the heartbeat covers that.
"""
import asyncio
from typing import Set

_subscribers: Set[asyncio.Queue] = set()

MAX_SUBSCRIBERS = 200  # hard cap — reject with 503 when exceeded


def subscribe() -> "asyncio.Queue | None":
    """Register a new SSE subscriber. Returns None if at capacity."""
    if len(_subscribers) >= MAX_SUBSCRIBERS:
        return None
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def publish(message: str) -> None:
    """Broadcast message to all connected SSE clients (fire-and-forget)."""
    for q in list(_subscribers):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass  # slow subscriber — drop rather than block the publisher


def subscriber_count() -> int:
    return len(_subscribers)
