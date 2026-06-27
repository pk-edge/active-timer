"""
POST /v1/ingest — high-throughput agent event receiver.

Flow
────
1. Validate the device JWT via the in-process auth cache (zero DB on the warm
   path — see app/core/device_auth_cache.py). The endpoint NEVER borrows a
   connection from the shared HTTP pool, so 1000s of agents posting every ~5s
   can never starve dashboard requests or overflow the pool.
2. Parse + clamp events (fast, in-process).
3. Update the in-memory device-state cache IMMEDIATELY (zero latency to the UI).
4. Enqueue the DB write — return 202 Accepted without waiting for the disk.
5. If the in-process queue is full (extreme sustained overload), return 503 so
   the agent applies its existing exponential backoff and retries later. The
   agent keeps every event in its local SQLite buffer and only marks them synced
   on a 2xx, so back-pressure is LOSSLESS — no event is ever dropped, and we
   never fall back to a synchronous DB write on the request path.

The net result: agents get near-instant 202s; the dashboard realtime feed
updates at enqueue time; DB writes are batched and executed by a bounded worker
pool on its own isolated connection pool.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_ingest_device_id
from app.core.app_normalizer import normalize_app_name
from app.core.config import get_settings
from app.core.ingest_queue import ingest_queue
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
    device_id: str = Depends(get_ingest_device_id),
):
    """
    Returns 202 Accepted as soon as events are validated and queued.
    The device state cache is updated synchronously inside enqueue() so the
    realtime dashboard endpoint reflects the new status with zero delay.

    No `Depends(get_db)` here on purpose: the hot ingest path must not hold an
    HTTP-pool connection. Auth is cached; the write is enqueued onto the
    dedicated ingest worker pool.
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
            "device_id":    device_id,
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
    # enqueue() updates the in-memory device cache immediately (no wait), then
    # drops the row on the async work queue. If the queue is full (rare — only
    # under extreme sustained overload), we return 503 so the agent backs off and
    # retries. The agent buffers events locally and only marks them synced on a
    # 2xx, so this is lossless — we never write synchronously on the HTTP pool.
    overflow = False
    for row in rows:
        if not ingest_queue.enqueue({"device_id": device_id, "row": row}):
            overflow = True
            break

    if overflow:
        log.warning("Ingest queue full — 503 backpressure (device=%s, queued_before_full)", device_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingest temporarily saturated — retry shortly",
            headers={"Retry-After": str(settings.INGEST_RETRY_AFTER_SECS)},
        )

    log.debug("device=%s queued=%d skipped=%d", device_id, len(rows), skipped)
    return IngestResponse(received=len(rows), skipped=skipped)
