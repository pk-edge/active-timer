import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from sqlalchemy.orm import joinedload

from app.api.deps import get_db, get_current_admin, require
from app.core import audit, broadcaster
from app.core.config import get_settings
from app.core.security import decode_token
from app.core.scoping import visible_device_ids, ALL
from app.db.models import Device, ActivityEvent, Employee
from app.schemas.device import DeviceOut, TimelineEventOut, DailyStatOut, AssignDeviceRequest

router   = APIRouter()
settings = get_settings()


async def _get_device_for_admin(device_id: str, admin: Employee, db: AsyncSession) -> Device:
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    result = await db.execute(
        select(Device).where(Device.id == device_uuid, Device.is_deleted == False)  # noqa: E712
    )
    device = result.scalar_one_or_none()
    if not device or device.company_id != admin.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    # Enforce data scope: a non-'all' user may only reach devices in their scope.
    ids = await visible_device_ids(admin, db)
    if ids is not ALL and device.id not in set(ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


@router.get("", response_model=List[DeviceOut])
async def list_devices(
    show_archived: bool = Query(default=False),
    admin: Employee = Depends(require("devices", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await visible_device_ids(admin, db)
    if ids is not ALL and not ids:
        return []

    if show_archived:
        # Active + archived, but never deleted
        q = (
            select(Device)
            .options(joinedload(Device.employee))
            .where(
                Device.company_id == admin.company_id,
                Device.is_deleted == False,  # noqa: E712
            )
            .order_by(Device.is_active.desc(), Device.last_seen.desc().nullslast())
        )
    else:
        q = (
            select(Device)
            .options(joinedload(Device.employee))
            .where(
                Device.company_id == admin.company_id,
                Device.is_active == True,    # noqa: E712
                Device.is_deleted == False,  # noqa: E712
            )
            .order_by(Device.last_seen.desc().nullslast())
        )

    if ids is not ALL:
        q = q.where(Device.id.in_(ids))
    result  = await db.execute(q)
    devices = result.scalars().all()
    cutoff  = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    return [
        DeviceOut(
            id=str(d.id),
            hostname=d.hostname,
            platform=d.platform,
            employee_id=str(d.employee_id) if d.employee_id else None,
            employee_name=d.employee.name if d.employee else None,
            last_seen=d.last_seen,
            last_seen_unix=int(d.last_seen.timestamp()) if d.last_seen else None,
            is_online=bool(d.last_seen and d.last_seen > cutoff),
            is_archived=d.is_archived,
            is_deleted=d.is_deleted,
        )
        for d in devices
    ]


@router.patch("/{device_id}/assign", response_model=DeviceOut)
async def assign_device(
    device_id: str,
    body: AssignDeviceRequest,
    admin: Employee = Depends(require("devices", "assign")),
    db: AsyncSession = Depends(get_db),
):
    """Assign a device to an employee, or unassign with employee_id=null.
    One employee may own any number of devices."""
    device = await _get_device_for_admin(device_id, admin, db)

    employee = None
    if body.employee_id:
        try:
            emp_uuid = uuid.UUID(body.employee_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid employee id")
        employee = (await db.execute(
            select(Employee).where(
                Employee.id == emp_uuid,
                Employee.company_id == admin.company_id,
                Employee.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        if not employee:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    device.employee_id = employee.id if employee else None
    await audit.record(
        db, admin,
        "device.assigned" if employee else "device.unassigned",
        "device", str(device.id),
        {"employee_id": str(employee.id) if employee else None, "source": "portal"},
    )
    await db.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    return DeviceOut(
        id=str(device.id),
        hostname=device.hostname,
        platform=device.platform,
        employee_id=str(employee.id) if employee else None,
        employee_name=employee.name if employee else None,
        last_seen=device.last_seen,
        last_seen_unix=int(device.last_seen.timestamp()) if device.last_seen else None,
        is_online=bool(device.last_seen and device.last_seen > cutoff),
        is_archived=device.is_archived,
        is_deleted=device.is_deleted,
    )


@router.patch("/{device_id}/archive", response_model=DeviceOut)
async def archive_device(
    device_id: str,
    admin: Employee = Depends(require("devices", "archive")),
    db: AsyncSession = Depends(get_db),
):
    """Suspend a device. It cannot send data until restored."""
    device = await _get_device_for_admin(device_id, admin, db)
    if device.is_archived:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is already archived")

    device.is_active   = False
    device.is_archived = True
    await audit.record(db, admin, "device.archived", "device", str(device.id), {"source": "portal"})
    await db.commit()

    return DeviceOut(
        id=str(device.id),
        hostname=device.hostname,
        platform=device.platform,
        employee_id=str(device.employee_id) if device.employee_id else None,
        employee_name=device.employee.name if device.employee else None,
        last_seen=device.last_seen,
        last_seen_unix=int(device.last_seen.timestamp()) if device.last_seen else None,
        is_online=False,
        is_archived=True,
        is_deleted=False,
    )


@router.patch("/{device_id}/restore", response_model=DeviceOut)
async def restore_device(
    device_id: str,
    admin: Employee = Depends(require("devices", "archive")),
    db: AsyncSession = Depends(get_db),
):
    """Restore an archived device to active status."""
    device = await _get_device_for_admin(device_id, admin, db)
    if not device.is_archived:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is not archived")

    device.is_active   = True
    device.is_archived = False
    await audit.record(db, admin, "device.restored", "device", str(device.id), {"source": "portal"})
    await db.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    return DeviceOut(
        id=str(device.id),
        hostname=device.hostname,
        platform=device.platform,
        employee_id=str(device.employee_id) if device.employee_id else None,
        employee_name=device.employee.name if device.employee else None,
        last_seen=device.last_seen,
        last_seen_unix=int(device.last_seen.timestamp()) if device.last_seen else None,
        is_online=bool(device.last_seen and device.last_seen > cutoff),
        is_archived=False,
        is_deleted=False,
    )


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: str,
    admin: Employee = Depends(require("devices", "delete")),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a device. No DB row is removed; the device disappears from the UI."""
    device = await _get_device_for_admin(device_id, admin, db)

    device.is_deleted  = True
    device.is_active   = False
    device.is_archived = False
    await audit.record(db, admin, "device.deleted", "device", str(device.id), {"source": "portal"})
    await db.commit()


@router.get("/stream")
async def stream_device_status(
    token: str = Query(..., description="Admin JWT — passed as query param because EventSource cannot set headers"),
):
    """SSE endpoint — pushes 'devices_updated' whenever any device syncs.

    Industrial-standard pattern:
    - Auth and DB work happen before streaming starts; connection released immediately.
    - No subscriber cap: events fan out via Redis pub/sub (app/core/broadcaster.py),
      so this scales across workers/replicas.
    - Burst coalescing: rapid ingest flushes are merged so the frontend receives
      at most one push every MIN_PUSH_INTERVAL seconds (same as home/stream).
    - 25 s heartbeat keeps proxies and load-balancers from killing idle connections.
    - CancelledError caught explicitly so client disconnect is always clean.
    """
    try:
        payload = decode_token(token)
        if payload.get("type") != "admin":
            raise ValueError
        user_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Short-lived session for auth only — no Depends(get_db): on a long-lived SSE
    # stream it would hold a pooled connection per open tab → pool exhaustion.
    from app.db.base import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Employee).where(Employee.id == user_uuid, Employee.is_active == True)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # The auth session above is already released (its `async with` block exited),
    # so the long-lived stream below holds NO pooled connection — no per-tab leak.
    MIN_PUSH_INTERVAL = 2.0  # seconds — matches home/stream throttle

    async def event_stream():
        q = broadcaster.subscribe()
        try:
            yield "event: connected\ndata: ok\n\n"
            last_push = time.monotonic()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    # Coalesce any burst that arrived while we were sleeping
                    # and enforce the minimum push interval.
                    delay = MIN_PUSH_INTERVAL - (time.monotonic() - last_push)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    while not q.empty():
                        msg = q.get_nowait()  # keep the most recent event name
                    yield f"event: {msg}\ndata: 1\n\n"
                    last_push = time.monotonic()
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass  # client disconnected — finally block handles cleanup
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/{device_id}/timeline", response_model=List[TimelineEventOut])
async def get_timeline(
    device_id: str,
    since:     Optional[int] = Query(default=None),
    until:     Optional[int] = Query(default=None),
    limit:     int = Query(default=500, le=2000),
    offset:    int = Query(default=0, ge=0),
    admin: Employee = Depends(require("devices", "view")),
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
    admin: Employee = Depends(require("devices", "view")),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_for_admin(device_id, admin, db)

    result = await db.execute(
        text("""
            SELECT
                DATE(timestamp) AS day,
                SUM(CASE
                    WHEN event_type = 'window_change'
                     AND app_name IS NOT NULL
                     AND app_name NOT IN (
                         'loginwindow','ScreenSaverEngine',
                         'System Idle Process','LockApp','LogonUI','Idle','screensaver'
                     )
                    THEN duration ELSE 0
                END) AS active,
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
