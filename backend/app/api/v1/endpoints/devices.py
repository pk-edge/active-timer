import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_current_admin
from app.core import broadcaster
from app.core.config import get_settings
from app.core.security import decode_token
from app.db.models import Device, ActivityEvent, Employee
from app.schemas.device import DeviceOut, TimelineEventOut, DailyStatOut

router   = APIRouter()
settings = get_settings()


async def _get_device_for_admin(device_id: str, admin: Employee, db: AsyncSession) -> Device:
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    result = await db.execute(select(Device).where(Device.id == device_uuid))
    device = result.scalar_one_or_none()
    if not device or device.company_id != admin.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


@router.get("", response_model=List[DeviceOut])
async def list_devices(
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Device)
        .options(selectinload(Device.employee))
        .where(Device.company_id == admin.company_id, Device.is_active == True)
        .order_by(Device.last_seen.desc().nullslast())
    )
    devices = result.scalars().all()
    cutoff  = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    return [
        DeviceOut(
            id=str(d.id),
            hostname=d.hostname,
            platform=d.platform,
            employee_name=d.employee.name if d.employee else None,
            last_seen=d.last_seen,
            last_seen_unix=int(d.last_seen.timestamp()) if d.last_seen else None,
            is_online=bool(d.last_seen and d.last_seen > cutoff),
        )
        for d in devices
    ]


@router.get("/stream")
async def stream_device_status(
    token: str = Query(..., description="Admin JWT — passed as query param because EventSource cannot set headers"),
    db: AsyncSession = Depends(get_db),
):
    """SSE endpoint — pushes 'devices_updated' whenever any device syncs."""
    try:
        payload = decode_token(token)
        if payload.get("type") != "admin":
            raise ValueError
        user_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(
        select(Employee).where(Employee.id == user_uuid, Employee.is_active == True)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    async def event_stream():
        q = broadcaster.subscribe()
        try:
            yield "event: connected\ndata: ok\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"event: {msg}\ndata: 1\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{device_id}/timeline", response_model=List[TimelineEventOut])
async def get_timeline(
    device_id: str,
    since:     Optional[int] = Query(default=None),
    until:     Optional[int] = Query(default=None),
    limit:     int = Query(default=500, le=2000),
    offset:    int = Query(default=0, ge=0),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_for_admin(device_id, admin, db)

    q = select(ActivityEvent).where(ActivityEvent.device_id == device.id)
    if since is not None:
        q = q.where(ActivityEvent.timestamp >= datetime.fromtimestamp(since, tz=timezone.utc))
    if until is not None:
        q = q.where(ActivityEvent.timestamp < datetime.fromtimestamp(until, tz=timezone.utc))

    result = await db.execute(
        q.order_by(ActivityEvent.timestamp.asc()).offset(offset).limit(limit)
    )
    return [
        TimelineEventOut(
            id=str(e.id), event_type=e.event_type,
            app_name=e.app_name, window_title=e.window_title,
            url=e.url, duration=e.duration, timestamp=e.timestamp,
            timestamp_unix=int(e.timestamp.timestamp()),
        )
        for e in result.scalars().all()
    ]


@router.get("/{device_id}/stats", response_model=List[DailyStatOut])
async def get_daily_stats(
    device_id: str,
    days:      int = Query(default=7, ge=1, le=90),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_for_admin(device_id, admin, db)

    result = await db.execute(
        text("""
            SELECT
                DATE(timestamp) AS day,
                SUM(CASE WHEN event_type NOT IN ('idle_start','lock') THEN duration ELSE 0 END) AS active,
                SUM(CASE WHEN event_type = 'idle_start' THEN duration ELSE 0 END) AS idle
            FROM activity_events
            WHERE device_id = :device_id
              AND timestamp >= NOW() - make_interval(days => :days)
            GROUP BY day
            ORDER BY day DESC
        """),
        {"device_id": str(device.id), "days": days},
    )
    return [
        DailyStatOut(
            date=str(r.day),
            active_seconds=int(r.active or 0),
            idle_seconds=int(r.idle or 0),
            total_seconds=int((r.active or 0) + (r.idle or 0)),
        )
        for r in result.fetchall()
    ]
