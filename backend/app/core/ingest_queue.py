"""
Ingest Queue — async work queue for high-throughput agent event writes.

Architecture
────────────
                ┌─ agent A ──► POST /v1/ingest
                ├─ agent B ──► POST /v1/ingest    → 202 Accepted immediately
                └─ agent N ──► POST /v1/ingest
                                    │
                                    ▼
                           ┌─────────────────┐
                           │  IngestQueue    │  bounded asyncio.Queue
                           │  (max 100 000)  │  – drops nothing while space exists
                           └────────┬────────┘  – back-pressure: sync write if full
                                    │
                      ┌─────────────┼──────────────┐
                      ▼             ▼              ▼
                  worker-0      worker-1  …   worker-N   (adaptive pool)
                      │
                      ▼
              bulk INSERT … ON CONFLICT DO NOTHING
              + UPDATE devices SET last_seen = NOW()
              + broadcaster.publish("devices_updated")

Pool sizing
───────────
• Starts with MIN_WORKERS (4).
• Every SCALE_INTERVAL seconds a monitor coroutine checks queue depth.
  – depth >  SCALE_UP_AT   → spin up one worker  (up to MAX_WORKERS = 16)
  – depth <  SCALE_DOWN_AT → shut down one worker (down to MIN_WORKERS)
• Workers drain in BATCH_SIZE chunks (default 200) so each DB round-trip
  is as cheap as possible.

Device-state cache
──────────────────
Workers also update an in-memory dict keyed by device_id with the latest
last_seen, current_app, event_type.  The realtime endpoint reads from this
dict instead of running the LATERAL-join SQL query on every poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings

log = logging.getLogger("ingest_queue")

# ── Tuning constants ──────────────────────────────────────────────────────────
QUEUE_MAXSIZE   = 100_000   # drop back-pressure threshold
BATCH_SIZE      = 200       # events per DB write cycle
BATCH_WAIT      = 0.4       # seconds to wait for a full batch before flushing
MIN_WORKERS     = 4         # always-on workers
MAX_WORKERS     = get_settings().INGEST_MAX_WORKERS  # env-driven; keep < DB pool
SCALE_INTERVAL  = 5.0       # how often to check queue depth
SCALE_UP_AT     = 500       # grow pool when queue exceeds this
SCALE_DOWN_AT   = 50        # shrink pool when queue drops below this


class IngestQueue:
    """Singleton service owned by the FastAPI app lifespan."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._workers: list[asyncio.Task] = []
        self._monitor_task: asyncio.Task | None = None
        self._running = False

        # In-memory device state cache: {device_id: {...}} used by realtime endpoint
        self.device_cache: dict[str, dict] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for _ in range(MIN_WORKERS):
            self._spawn_worker()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="ingest-monitor"
        )
        log.info("IngestQueue started (min=%d max=%d batch=%d)",
                 MIN_WORKERS, MAX_WORKERS, BATCH_SIZE)

    async def stop(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        # Drain remaining items with a final flush
        await self._flush_remaining()
        for t in self._workers:
            t.cancel()
        self._workers.clear()
        log.info("IngestQueue stopped")

    # ── Enqueue ───────────────────────────────────────────────────────────────

    def enqueue(self, item: dict[str, Any]) -> bool:
        """Non-blocking enqueue.

        The device state cache is updated HERE, before the DB write, so the
        realtime endpoint always reflects the latest agent status with zero
        latency regardless of how busy the worker pool is.

        Returns False (but still updates cache) if the queue is full — the
        caller can fall back to a synchronous write for that batch.
        """
        # Cache update is instant; never skip it.
        self._update_cache(item)
        try:
            self._q.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    # ── Worker pool management ────────────────────────────────────────────────

    def _spawn_worker(self) -> None:
        n = len(self._workers)
        t = asyncio.create_task(self._worker(n), name=f"ingest-worker-{n}")
        t.add_done_callback(lambda _t: self._workers.remove(_t)
                            if _t in self._workers else None)
        self._workers.append(t)
        log.debug("Spawned ingest worker %d (pool size=%d)", n, len(self._workers))

    async def _monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(SCALE_INTERVAL)
            depth = self._q.qsize()
            alive = len([t for t in self._workers if not t.done()])

            if depth > SCALE_UP_AT and alive < MAX_WORKERS:
                self._spawn_worker()
                log.info("IngestQueue scaled UP → %d workers (queue depth %d)",
                         alive + 1, depth)

            elif depth < SCALE_DOWN_AT and alive > MIN_WORKERS:
                # Mark one worker for graceful exit on next empty wait
                self._workers[0].cancel()
                log.info("IngestQueue scaled DOWN → %d workers (queue depth %d)",
                         alive - 1, depth)

    # ── Worker coroutine ──────────────────────────────────────────────────────

    async def _worker(self, worker_id: int) -> None:
        from app.db.base import AsyncSessionLocal
        from app.core import broadcaster
        from app.db.models import ActivityEvent
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import text

        log.debug("Worker %d starting", worker_id)
        try:
            while True:
                batch: list[dict] = []

                # Block until at least one item arrives
                try:
                    item = await asyncio.wait_for(self._q.get(), timeout=1.0)
                    batch.append(item)
                    self._q.task_done()
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    if not self._running:
                        break
                    continue

                # Greedily collect up to BATCH_SIZE without extra waiting
                deadline = time.monotonic() + BATCH_WAIT
                while len(batch) < BATCH_SIZE and time.monotonic() < deadline:
                    try:
                        extra = self._q.get_nowait()
                        batch.append(extra)
                        self._q.task_done()
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.02)

                # --- DB write ---
                rows = [b["row"] for b in batch]
                device_ids = list({b["device_id"] for b in batch})

                try:
                    async with AsyncSessionLocal() as db:
                        # Bulk insert events
                        stmt = (
                            pg_insert(ActivityEvent)
                            .values(rows)
                            .on_conflict_do_nothing(index_elements=["id"])
                        )
                        await db.execute(stmt)

                        # Touch last_seen for each device that appeared in this batch
                        await db.execute(
                            text("UPDATE devices SET last_seen = NOW() "
                                 "WHERE id = ANY(:ids)"),
                            {"ids": device_ids},
                        )
                        await db.commit()

                    broadcaster.publish("devices_updated")

                    log.debug("Worker %d: flushed %d events, %d devices",
                              worker_id, len(rows), len(device_ids))

                except Exception as exc:
                    log.error("Worker %d DB write failed: %s", worker_id, exc)

        except asyncio.CancelledError:
            pass
        log.debug("Worker %d stopped", worker_id)

    # ── Device state cache ────────────────────────────────────────────────────

    def _update_cache(self, item: dict) -> None:
        did = item["device_id"]
        row = item["row"]
        existing = self.device_cache.get(did, {})
        ts = row.get("timestamp")
        cached_ts = existing.get("last_seen")

        # Only update if this event is newer than what we have
        if ts and (cached_ts is None or ts > cached_ts):
            self.device_cache[did] = {
                "last_seen":    ts,
                "latest_type":  row.get("event_type"),
                "current_app":  row.get("app_name") if row.get("event_type") == "window_change" else existing.get("current_app"),
                "updated_at":   time.time(),
            }

    # ── Drain on shutdown ─────────────────────────────────────────────────────

    async def _flush_remaining(self) -> None:
        remaining = self._q.qsize()
        if remaining == 0:
            return
        log.info("Flushing %d remaining items on shutdown", remaining)
        try:
            from app.db.base import AsyncSessionLocal
            from app.db.models import ActivityEvent
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy import text

            rows, device_ids = [], set()
            while not self._q.empty():
                try:
                    b = self._q.get_nowait()
                    rows.append(b["row"])
                    device_ids.add(b["device_id"])
                    self._q.task_done()
                except asyncio.QueueEmpty:
                    break

            if rows:
                async with AsyncSessionLocal() as db:
                    stmt = (
                        pg_insert(ActivityEvent)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    await db.execute(stmt)
                    await db.execute(
                        text("UPDATE devices SET last_seen = NOW() WHERE id = ANY(:ids)"),
                        {"ids": list(device_ids)},
                    )
                    await db.commit()
        except Exception as e:
            log.error("Flush-on-shutdown failed: %s", e)


# Singleton — imported by ingest endpoint and wired into app lifespan
ingest_queue = IngestQueue()
