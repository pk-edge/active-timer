"""
POST /v1/ingest — high-throughput agent event receiver.

Flow
────
1. Validate JWT + parse events (fast, in-process).
2. Update in-memory device state cache IMMEDIATELY (zero latency to UI).
3. Enqueue the DB write — return 202 Accepted without waiting for the disk.
4. If the in-process queue is full (extreme overload), fall back to a
   direct synchronous write so no events are silently dropped.

The net result: thousands of concurrent agents get near-instant 202 responses;
the dashboard realtime feed updates at enqueue time; DB writes are batched and
executed by a bounded worker pool that auto-scales with queue depth.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import get_db, get_current_device
from app.core import broadcaster
from app.core.app_normalizer import normalize_app_name
from app.core.config import get_settings
from app.core.ingest_queue import ingest_queue
from app.db.models import Device, ActivityEvent
from app.schemas.event import ALLOWED_EVENT_TYPES, IngestRequest, IngestResponse

log = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


def capped_duration(event_type: str, duration: int, max_window_slice: int) -> int:
    """Clamp a single window_change slice to a sane maximum.

    The agent emits one window_change per heartbeat (~60s), so a legit slice is
    tiny. A sleeping/offline/old agent can report hours against one window
    ("time keeps counting") — clamp those. Other event types (idle_end, lock,
    meeting_end) carry genuinely long durations and are never capped.
    """
    if event_type == "window_change" and duration > max_window_slice:
        return max_window_slice
    return duration


@router.post("", status_code=202)
async def ingest_events(
    payload: IngestRequest,
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns 202 Accepted as soon as events are validated and queued.
    The device state cache is updated synchronously inside enqueue() so
    the realtime dashboard endpoint reflects the new status with zero delay.
    """
    if not payload.events:
        return IngestResponse(received=0, skipped=0)

    rows, skipped = [], 0
    for e in payload.events:
        if e.event_type not in ALLOWED_EVENT_TYPES:
            skipped += 1
            continue
        try:
            if isinstance(e.timestamp, int):
                ts = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
        except (ValueError, OSError):
            skipped += 1
            continue
        rows.append({
            "id":           e.id,
            "device_id":    str(device.id),
            "event_type":   e.event_type,
            "app_name":     normalize_app_name(e.app_name),
            "window_title": (e.window_title or "")[:500],
            "url":          (e.url or "")[:2000],
            "duration":     capped_duration(e.event_type, e.duration, settings.MAX_WINDOW_SLICE_SECS),
            "timestamp":    ts,
        })

    if not rows:
        return IngestResponse(received=0, skipped=skipped)

    # ── Enqueue each row ──────────────────────────────────────────────────────
    # enqueue() updates the in-memory device cache immediately (no wait),
    # then drops the row on the async work queue.  If the queue is full
    # (rare — only under extreme sustained load) we fall back to a direct
    # synchronous write for this batch so data is never silently dropped.
    overflow_rows = []
    for row in rows:
        item = {"device_id": str(device.id), "row": row}
        if not ingest_queue.enqueue(item):
            overflow_rows.append(row)

    if overflow_rows:
        log.warning("Queue full — synchronous fallback for %d events (device=%s)",
                    len(overflow_rows), device.id)
        await _sync_write(overflow_rows, str(device.id), db)

    log.debug("device=%s queued=%d overflow=%d skipped=%d",
              device.id, len(rows) - len(overflow_rows), len(overflow_rows), skipped)

    return IngestResponse(received=len(rows), skipped=skipped)


async def _sync_write(rows: list[dict], device_id: str, db: AsyncSession) -> None:
    """Direct synchronous write used only when the async queue is full."""
    stmt = (
        pg_insert(ActivityEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.execute(stmt)
    await db.execute(
        text("UPDATE devices SET last_seen = NOW() WHERE id = :id"),
        {"id": device_id},
    )
    await db.commit()
    await broadcaster.publish("devices_updated")
