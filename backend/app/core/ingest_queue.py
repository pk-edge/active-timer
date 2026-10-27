"""
Ingest Queue — 30-second bulk COPY accumulator for agent event writes.

Architecture
────────────
               ┌─ agent A ──► POST /v1/ingest
               ├─ agent B ──► POST /v1/ingest    → 202 Accepted immediately
               └─ agent N ──► POST /v1/ingest
                                   │
                    enqueue() updates device_cache instantly (zero latency to UI)
                                   │
                                   ▼
                          ┌─────────────────┐
                          │  asyncio.Queue  │  bounded 100 000 events
                          │  (in-memory)    │  503 back-pressure if full
                          └────────┬────────┘
                                   │
                              every 30 s
                                   │
                                   ▼
                          ┌─────────────────────────────────┐
                          │  PostgreSQL COPY bulk flush      │
                          │                                 │
                          │  COPY rows → temp _ae_stage     │  fastest bulk write path
                          │  INSERT … SELECT … ON CONFLICT  │  dedup by event id
                          │  UPDATE devices SET last_seen   │  one query for all devices
                          └─────────────────────────────────┘

Why COPY + 30s window
─────────────────────
At 2 000 agents × 1 event/5 s = 400 events/second, a 0.4-second batch window
produces ~160 events per flush → ~1 000 DB round-trips/minute.  A 30-second
window accumulates ~12 000 events and yields 2 round-trips/minute — 500× fewer
commits, 500× less lock contention, and PostgreSQL COPY is 3-5× faster than
bulk INSERT for large payloads (no parse/plan overhead per row, minimal WAL).

Because COPY has no ON CONFLICT clause, we stage into a temp table first:
  • CREATE TEMP TABLE IF NOT EXISTS _ae_stage … ON COMMIT DELETE ROWS
    (created once per connection; ON COMMIT DELETE ROWS self-clears after each
    transaction, so the same table can be reused across flushes on the same
    connection without a TRUNCATE)
  • COPY records → _ae_stage
  • INSERT INTO activity_events SELECT … FROM _ae_stage ON CONFLICT (id) DO NOTHING
  • UPDATE devices SET last_seen = NOW() WHERE id = ANY(…)

The device-state cache (used by the realtime dashboard) is updated at enqueue
time — never delayed by the 30-second window.  The UI stays instant.

Durability
──────────
• Agents get 202 immediately.  The backend owns events from that point.
• On transient DB failure (network hiccup, failover): the whole batch is
  re-queued without incrementing a retry counter, so a multi-minute outage
  never exhausts the budget.
• On non-transient failure (poison row): row-by-row rescue so one bad event
  cannot sink 12 000 good ones.
• On server crash mid-flush: any items already dequeued but not yet written are
  re-queued by the CancelledError handler in _flush_now, then written by the
  final flush in stop() — no silent loss.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings

log = logging.getLogger("ingest_queue")

# ── Tuning ────────────────────────────────────────────────────────────────────

FLUSH_INTERVAL      = 30.0      # seconds between bulk COPY flushes
QUEUE_MAXSIZE       = 100_000   # 503 back-pressure above this
MAX_WRITE_RETRIES   = 3         # poison-pill guard (non-transient errors only)
TRANSIENT_RETRY_BACKOFF = 1.0   # seconds before re-flushing after a transient error

_settings = get_settings()

# ── Staging table (created once per DB connection, self-clearing on commit) ───

_STAGE_DDL = """\
CREATE TEMP TABLE IF NOT EXISTS _ae_stage (
    id            UUID         NOT NULL,
    device_id     UUID         NOT NULL,
    event_type    TEXT         NOT NULL,
    app_name      TEXT,
    window_title  TEXT,
    url           TEXT,
    duration      INTEGER      NOT NULL DEFAULT 0,
    timestamp     TIMESTAMPTZ  NOT NULL,
    received_at   TIMESTAMPTZ  NOT NULL
) ON COMMIT DELETE ROWS\
"""

_STAGE_COLS = [
    "id", "device_id", "event_type",
    "app_name", "window_title", "url",
    "duration", "timestamp", "received_at",
]

_MERGE_SQL = """\
INSERT INTO activity_events
    (id, device_id, event_type, app_name, window_title, url,
     duration, timestamp, received_at)
SELECT id, device_id, event_type, app_name, window_title, url,
       duration, timestamp, received_at
FROM _ae_stage
ON CONFLICT (id) DO NOTHING\
"""

_LAST_SEEN_SQL = (
    "UPDATE devices SET last_seen = NOW() WHERE id = ANY($1::uuid[])"
)


class IngestQueue:
    """Singleton service owned by the FastAPI app lifespan."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._flusher_task: asyncio.Task | None = None
        self._running = False

        # In-memory device state cache: {device_id: {...}}
        # Read by the realtime SSE endpoint — updated at enqueue time, never
        # delayed by the flush interval.
        self.device_cache: dict[str, dict] = {}
        # Devices that sent a bare presence heartbeat (empty ingest POST) since
        # the last flush — their devices.last_seen is refreshed in one UPDATE.
        self._touched: set[str] = set()
        # Optional hook(device_id, state) fired on every cache update — wired in
        # main.py to DeviceStateSync so sibling uvicorn workers see the state.
        self.on_state_update = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._flusher_task = asyncio.create_task(
            self._flusher_loop(), name="ingest-flusher"
        )
        log.info(
            "IngestQueue started  flush_interval=%.0fs  queue_max=%d",
            FLUSH_INTERVAL, QUEUE_MAXSIZE,
        )

    async def stop(self) -> None:
        self._running = False
        if self._flusher_task and not self._flusher_task.done():
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass
        # Final flush: picks up any events re-queued by CancelledError handler
        # inside _flush_now, plus anything that arrived between the last timer
        # flush and shutdown.
        await self._flush_now(label="shutdown")
        log.info("IngestQueue stopped")

    # ── Public: enqueue ───────────────────────────────────────────────────────

    def enqueue(self, item: dict[str, Any]) -> bool:
        """Non-blocking enqueue.

        Updates the in-memory device cache IMMEDIATELY — zero latency to the
        realtime dashboard regardless of the 30-second DB flush interval.

        Returns False if the queue is full (caller should return 503).
        """
        self._update_cache(item)
        try:
            self._q.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    def touch(self, device_id: str) -> None:
        """Record "this device is alive right now" without any activity event.

        Agents that are idle, screen-locked or simply have nothing queued send an
        empty ingest POST as a heartbeat. The authoritative effect is the
        devices.last_seen UPDATE at the next flush (every reader's online check
        honours it, on every worker). Locally, an EXISTING cache entry also gets
        its presence bumped to server time so this worker's realtime view reacts
        immediately — its latest_type / current_app are left alone, so an
        idle/locked device stays "passive".

        Deliberately not done: creating a cache entry for an unknown device
        (there is no state to show — it would read as "active") and pushing to
        DeviceStateSync (a heartbeat carries no state, so it must never
        overwrite a sibling's newer latest_type in Redis).
        """
        self._touched.add(device_id)
        existing = self.device_cache.get(device_id)
        if existing is None:
            return
        now = datetime.now(timezone.utc)
        cached_ts = existing.get("last_seen")
        if cached_ts is None or now > cached_ts:
            existing["last_seen"] = now
            existing["updated_at"] = time.time()

    # ── Timer loop ────────────────────────────────────────────────────────────

    async def _flusher_loop(self) -> None:
        """Sleep FLUSH_INTERVAL, flush, repeat.  Exits cleanly on cancellation."""
        try:
            while self._running:
                await asyncio.sleep(FLUSH_INTERVAL)
                if not self._running:
                    break
                await self._flush_now()
        except asyncio.CancelledError:
            pass

    # ── Flush ─────────────────────────────────────────────────────────────────

    async def _flush_now(self, label: str = "timer") -> None:
        """Drain the queue and bulk-write everything via PostgreSQL COPY."""
        batch: list[dict] = []
        while True:
            try:
                item = self._q.get_nowait()
                batch.append(item)
                self._q.task_done()
            except asyncio.QueueEmpty:
                break

        touched, self._touched = self._touched, set()

        if not batch:
            if touched and await self._touch_to_db(touched):
                from app.core import broadcaster
                await broadcaster.publish("devices_updated")
            return

        rows       = [b["row"]       for b in batch]
        # Heartbeat-only devices ride the same transaction's last_seen UPDATE.
        device_ids = list({b["device_id"] for b in batch} | touched)

        log.debug("[%s] flushing %d events, %d devices", label, len(rows), len(device_ids))

        try:
            await self._copy_to_db(rows, device_ids)
            from app.core import broadcaster
            await broadcaster.publish("devices_updated")
            log.info("[%s] COPY flush done: %d events, %d devices", label, len(rows), len(device_ids))

        except asyncio.CancelledError:
            # The DB write was interrupted (shutdown during flush).
            # Put everything back so the final stop() flush can write it.
            self._touched |= touched
            for item in reversed(batch):
                try:
                    self._q.put_nowait(item)
                except asyncio.QueueFull:
                    self._dead_letter(item, "queue full during cancellation recovery")
            raise

        except Exception as exc:
            self._touched |= touched     # heartbeat-only devices retry next flush
            await self._handle_write_failure(batch, exc)

    async def _touch_to_db(self, device_ids: set[str]) -> bool:
        """Presence-only write: one UPDATE for every heartbeat-only device.

        Best-effort — a failure re-arms the ids for the next flush. Returns True
        when the UPDATE committed.
        """
        from app.db.base import ingest_engine

        try:
            async with ingest_engine.connect() as conn:
                raw = await conn.get_raw_connection()
                pg = raw.driver_connection  # asyncpg.Connection
                await pg.execute(
                    _LAST_SEEN_SQL,
                    [uuid.UUID(d) if isinstance(d, str) else d for d in device_ids],
                )
        except asyncio.CancelledError:
            self._touched |= device_ids
            raise
        except Exception as exc:  # noqa: BLE001
            self._touched |= device_ids
            log.warning("Heartbeat last_seen update failed (%d device(s)): %s", len(device_ids), exc)
            return False
        return True

    # ── PostgreSQL COPY write ─────────────────────────────────────────────────

    async def _copy_to_db(self, rows: list[dict], device_ids: list[str]) -> None:
        """
        Bulk-write via COPY into temp staging table, then merge into activity_events.

        Uses raw asyncpg (via the SQLAlchemy ingest engine) so copy_records_to_table
        is available — SQLAlchemy's execute() layer doesn't expose COPY natively.

        Transaction: all three statements (COPY + INSERT + UPDATE) are atomic.
        If anything fails, PostgreSQL rolls back; the caller re-queues the batch.
        """
        from app.db.base import ingest_engine

        now = datetime.now(timezone.utc)

        def _uuid(v: Any) -> uuid.UUID:
            return uuid.UUID(v) if isinstance(v, str) else v

        records = [
            (
                _uuid(r["id"]),
                _uuid(r["device_id"]),
                r["event_type"],
                r["app_name"],
                r["window_title"] or "",
                r["url"] or "",
                r["duration"],
                r["timestamp"],
                now,
            )
            for r in rows
        ]
        did_uuids = [_uuid(d) for d in device_ids]

        async with ingest_engine.connect() as conn:
            raw = await conn.get_raw_connection()
            pg = raw.driver_connection  # asyncpg.Connection

            async with pg.transaction():
                # Temp table: created once per connection, rows cleared on commit.
                await pg.execute(_STAGE_DDL)
                await pg.copy_records_to_table(
                    "_ae_stage",
                    records=records,
                    columns=_STAGE_COLS,
                )
                await pg.execute(_MERGE_SQL)
                await pg.execute(_LAST_SEEN_SQL, did_uuids)

    # ── Write failure handling ────────────────────────────────────────────────

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError
        if isinstance(exc, (OperationalError, InterfaceError)):
            return True
        if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
            return True
        try:
            import asyncpg
            if isinstance(exc, (
                asyncpg.ConnectionFailureError,
                asyncpg.ConnectionDoesNotExistError,
                asyncpg.TooManyConnectionsError,
                asyncpg.PostgresConnectionError,
            )):
                return True
        except ImportError:
            pass
        return False

    async def _handle_write_failure(self, batch: list[dict], exc: Exception) -> None:
        """Recover a failed batch WITHOUT losing events.

        • Transient (DB failover / outage): re-enqueue the whole batch without
          incrementing the retry counter — a multi-minute outage must not burn
          through the budget and dead-letter valid events.
        • Non-transient (constraint / data error): fall back to row-by-row rescue
          so one poison event cannot sink thousands of valid ones.
        """
        if self._is_transient(exc):
            await asyncio.sleep(TRANSIENT_RETRY_BACKOFF)
            n = self._requeue(batch, count_attempt=False)
            log.warning("Transient write error (%s) — requeued %d events for retry", exc, n)
            return
        await self._rescue_per_row(batch, exc)

    def _requeue(self, items: list[dict], count_attempt: bool) -> int:
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

    async def _rescue_per_row(self, batch: list[dict], exc: Exception) -> None:
        """Write each event individually so one bad row can't sink the rest.

        Uses SQLAlchemy INSERT (not COPY) — one row at a time, each in its own
        transaction.  Slow, but only triggered by non-transient data errors.
        """
        from app.db.base import IngestSessionLocal
        from app.db.models import ActivityEvent
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        log.warning(
            "Non-transient write error (%s) — isolating %d events row-by-row",
            exc, len(batch),
        )
        ok_devices: set[str] = set()
        poison = 0
        for item in batch:
            try:
                async with IngestSessionLocal() as db:
                    await db.execute(
                        pg_insert(ActivityEvent)
                        .values([item["row"]])
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
                    await db.commit()
                ok_devices.add(item["device_id"])
            except Exception as row_exc:
                if self._is_transient(row_exc):
                    self._requeue([item], count_attempt=False)
                else:
                    self._dead_letter(item, str(row_exc))
                    poison += 1

        if ok_devices:
            try:
                from sqlalchemy import text
                async with IngestSessionLocal() as db:
                    await db.execute(
                        text("UPDATE devices SET last_seen = NOW() WHERE id = ANY(:ids)"),
                        {"ids": list(ok_devices)},
                    )
                    await db.commit()
                from app.core import broadcaster
                await broadcaster.publish("devices_updated")
            except Exception as e:
                log.warning("Row-by-row rescue: last_seen update failed: %s", e)

        if poison:
            log.error("Dead-lettered %d poison event(s) after row-by-row rescue", poison)

    def _dead_letter(self, item: dict, reason: str) -> None:
        """Last resort: log full payload at ERROR so it's recoverable from logs."""
        log.error(
            "INGEST_DEAD_LETTER reason=%s device=%s row=%r",
            reason, item.get("device_id"), item.get("row"),
        )

    # ── Device state cache ────────────────────────────────────────────────────

    def _update_cache(self, item: dict) -> None:
        did = item["device_id"]
        row = item["row"]
        existing   = self.device_cache.get(did, {})
        ts         = row.get("timestamp")
        # Order by EVENT time. last_seen may hold a heartbeat's server-time stamp
        # (touch()), which must not make a newer real event look stale.
        cached_ts  = existing.get("event_ts") or existing.get("last_seen")

        if ts and (cached_ts is None or ts > cached_ts):
            seen = existing.get("last_seen")
            self.device_cache[did] = {
                "last_seen":   ts if seen is None or ts > seen else seen,
                "event_ts":    ts,
                "latest_type": row.get("event_type"),
                "current_app": (
                    row.get("app_name")
                    if row.get("event_type") == "window_change"
                    else existing.get("current_app")
                ),
                "updated_at":  time.time(),
            }
            if self.on_state_update is not None:
                try:
                    self.on_state_update(did, self.device_cache[did])
                except Exception:  # noqa: BLE001 — sharing is best-effort, never break ingest
                    pass


# Singleton — imported by ingest endpoint and wired into the FastAPI lifespan
ingest_queue = IngestQueue()
