import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import get_db, get_current_device
from app.core import broadcaster
from app.db.models import Device
from app.schemas.event import IngestRequest, IngestResponse

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("", response_model=IngestResponse)
async def ingest_events(
    payload: IngestRequest,
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    if not payload.events:
        return IngestResponse(received=0, skipped=0)

    rows = []
    for e in payload.events:
        try:
            if isinstance(e.timestamp, int):
                ts = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
        except (ValueError, OSError):
            continue
        rows.append({
            "id":           e.id,
            "device_id":    str(device.id),
            "event_type":   e.event_type,
            "app_name":     e.app_name,
            "window_title": (e.window_title or "")[:500],
            "url":          (e.url or "")[:2000],
            "duration":     e.duration,
            "timestamp":    ts,
        })

    if not rows:
        return IngestResponse(received=0, skipped=len(payload.events))

    inserted = 0
    for row in rows:
        result = await db.execute(
            text("""
                INSERT INTO activity_events
                    (id, device_id, event_type, app_name, window_title, url, duration, timestamp)
                VALUES (:id, :device_id, :event_type, :app_name, :window_title, :url, :duration, :timestamp)
                ON CONFLICT (id) DO NOTHING
            """),
            row,
        )
        inserted += result.rowcount or 0

    await db.execute(
        text("UPDATE devices SET last_seen = NOW() WHERE id = :id"),
        {"id": str(device.id)},
    )
    await db.commit()
    broadcaster.publish("devices_updated")

    skipped = len(rows) - inserted
    log.info("device=%s inserted=%d skipped=%d", device.id, inserted, skipped)
    return IngestResponse(received=inserted, skipped=skipped)
