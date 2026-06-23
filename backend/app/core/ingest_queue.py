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
• Starts with MIN_WORKERS (INGEST_MIN_WORKERS, default 2).
• Every SCALE_INTERVAL seconds a monitor coroutine checks queue depth.
  – depth >  SCALE_UP_AT   → spin up one worker  (up to MAX_WORKERS)
  – depth <  SCALE_DOWN_AT → shut down one worker (down to MIN_WORKERS)
• MAX_WORKERS is hard-clamped to the ingest pool's connection count
  (INGEST_DB_POOL_SIZE + INGEST_DB_MAX_OVERFLOW) so workers can never
  outnumber connections and block on checkout.
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
MAX_WRITE_RETRIES = 3       # cap re-enqueues of a row that fails for a DATA reason
                            # (poison-pill guard) before dead-lettering it
TRANSIENT_RETRY_BACKOFF = 1.0  # seconds to wait before retrying a batch after a
                               # TRANSIENT DB error (failover/outage) so workers
                               # don't hot-loop against a down database
_settings        = get_settings()
MIN_WORKERS     = _settings.INGEST_MIN_WORKERS   # always-on workers
# Hard ceiling: never run more workers than the ingest pool has connections, or
# the surplus workers block on checkout and time out (the pool "overflow" jam).
# Keep MAX >= MIN so the pool is always at least its floor size.
_INGEST_CONN_CAP = _settings.INGEST_DB_POOL_SIZE + _settings.INGEST_DB_MAX_OVERFLOW
MAX_WORKERS     = max(MIN_WORKERS, min(_settings.INGEST_MAX_WORKERS, _INGEST_CONN_CAP))
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
        # Graceful scale-down: workers exit between batches when this is > 0,
        # instead of being cancelled mid-DB-write (which would drop the batch).
        self._shrink_pending = 0

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

            elif depth < SCALE_DOWN_AT and alive > MIN_WORKERS and self._shrink_pending == 0:
                # Ask one worker to exit after its current batch — never cancel
                # mid-write, or the batch it already popped would be lost.
                self._shrink_pending = 1
                log.info("IngestQueue scaling DOWN → %d workers (queue depth %d)",
                         alive - 1, depth)

    # ── Worker coroutine ──────────────────────────────────────────────────────

    async def _worker(self, worker_id: int) -> None:
        # Dedicated writer pool — never competes with HTTP requests for connections.
        from app.db.base import IngestSessionLocal
        from app.core import broadcaster
        from app.db.models import ActivityEvent
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import text

        log.debug("Worker %d starting", worker_id)
        try:
            while True:
                # Honour a pending scale-down between batches (safe exit point).
                if self._shrink_pending > 0:
                    alive = len([t for t in self._workers if not t.done()])
                    if alive > MIN_WORKERS:
                        self._shrink_pending -= 1
                        break

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
                    async with IngestSessionLocal() as db:
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

                    await broadcaster.publish("devices_updated")

                    log.debug("Worker %d: flushed %d events, %d devices",
                              worker_id, len(rows), len(device_ids))

                except Exception as exc:
                    # The agent already got its 202 and dropped these events from
                    # its local buffer, so the backend now owns them — we must NOT
                    # silently lose them. Recover based on WHY the write failed.
                    await self._handle_write_failure(batch, worker_id, exc)

        except asyncio.CancelledError:
            pass
        log.debug("Worker %d stopped", worker_id)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """True for connection/operational errors (failover, saturation, deadlock)
        that a retry can fix — as opposed to a data/constraint error that never
        will."""
        from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
        if isinstance(exc, (OperationalError, InterfaceError)):
            return True
        if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
            return True
        return False

    async def _handle_write_failure(self, batch: list[dict], worker_id: int, exc: Exception) -> None:
        """Recover a failed batch WITHOUT losing events.

        • Transient error (DB failover/outage/saturation): re-enqueue the whole
          batch UNCOUNTED after a short backoff, so a multi-minute outage never
          burns through a retry budget and drops data. The 100k queue + upstream
          503 back-pressure bound memory while the DB is down.
        • Non-transient error (a constraint/data problem): a single poison row
          would otherwise fail the whole 200-row bulk insert and take 199 good
          events with it — so fall back to writing each row on its OWN
          transaction. Good rows commit; only a genuinely bad row is isolated and
          dead-lettered (logged with full payload, recoverable), never silently
          dropped.
        """
        if self._is_transient(exc):
            await asyncio.sleep(TRANSIENT_RETRY_BACKOFF)
            requeued = self._requeue(batch, count_attempt=False)
            log.warning("Worker %d transient write error (%s) — requeued %d for retry",
                        worker_id, exc, requeued)
            return
        await self._rescue_per_row(batch, worker_id, exc)

    def _requeue(self, items: list[dict], count_attempt: bool) -> int:
        """Put items back on the queue. When count_attempt is True, a row that has
        exceeded MAX_WRITE_RETRIES is dead-lettered instead of requeued."""
        n = 0
        for item in items:
            if count_attempt:
                item["attempts"] = item.get("attempts", 0) + 1
                if item["attempts"] > MAX_WRITE_RETRIES:
                    self._dead_letter(item, "exceeded max write retries")
                    continue
            try:
                self._q.put_nowait(item)
                n += 1
            except asyncio.QueueFull:
                self._dead_letter(item, "queue full on requeue")
        return n

    async def _rescue_per_row(self, batch: list[dict], worker_id: int, exc: Exception) -> None:
        """Write each row in its own transaction so one poison row can't sink the
        rest of the batch."""
        from app.db.base import IngestSessionLocal
        from app.core import broadcaster
        from app.db.models import ActivityEvent
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy import text

        log.warning("Worker %d non-transient write error (%s) — isolating %d rows row-by-row",
                    worker_id, exc, len(batch))
        ok_devices: set[str] = set()
        poison = 0
        for item in batch:
            try:
                async with IngestSessionLocal() as db:
                    await db.execute(
                        pg_insert(ActivityEvent).values([item["row"]])
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    await db.commit()
                ok_devices.add(item["device_id"])
            except Exception as row_exc:
                if self._is_transient(row_exc):
                    # DB went down mid-rescue — requeue this row (uncounted) to
                    # retry once it recovers; do not dead-letter a transient error.
                    self._requeue([item], count_attempt=False)
                else:
                    self._dead_letter(item, str(row_exc))
                    poison += 1

        if ok_devices:
            try:
                async with IngestSessionLocal() as db:
                    await db.execute(
                        text("UPDATE devices SET last_seen = NOW() WHERE id = ANY(:ids)"),
                        {"ids": list(ok_devices)},
                    )
                    await db.commit()
                await broadcaster.publish("devices_updated")
            except Exception as last_seen_exc:
                log.warning("Worker %d rescue last_seen update failed: %s", worker_id, last_seen_exc)

        if poison:
            log.error("Worker %d dead-lettered %d poison row(s) after row-by-row rescue",
                      worker_id, poison)

    def _dead_letter(self, item: dict, reason: str) -> None:
        """Last resort for an unwritable event: log the FULL payload at ERROR so it
        is recoverable from logs (grep + replay) rather than vanishing silently.
        Intentionally no new table/infra — keeps the durability net architecture-
        light while making any loss loud and auditable."""
        log.error("INGEST_DEAD_LETTER reason=%s device=%s row=%r",
                  reason, item.get("device_id"), item.get("row"))

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
            from app.db.base import IngestSessionLocal
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
                async with IngestSessionLocal() as db:
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
