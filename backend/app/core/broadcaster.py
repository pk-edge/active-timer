"""
In-process pub/sub for SSE push notifications.

Each SSE subscriber gets its own asyncio.Queue. When the ingest endpoint
updates last_seen it calls publish() — every connected dashboard tab
receives the event and triggers an immediate SWR re-fetch.
"""
import asyncio
from typing import Set

_subscribers: Set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
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
            pass
