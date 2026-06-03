import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.api.deps import get_db, get_current_admin
from app.db.models import Device, Employee
from app.schemas.report import CompanyDayOut, AppUsageOut, UrlVisitOut

router = APIRouter()


@router.get("/company/summary", response_model=List[CompanyDayOut])
async def company_summary(
    days:  int = Query(default=7, ge=1, le=90),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""
            SELECT
                DATE(ae.timestamp) AS day,
                SUM(CASE WHEN ae.event_type NOT IN ('idle_start','lock') THEN ae.duration ELSE 0 END) AS active,
                SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle,
                COUNT(DISTINCT ae.device_id) AS device_count
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.timestamp >= NOW() - make_interval(days => :days)
            GROUP BY day ORDER BY day DESC
        """),
        {"company_id": str(admin.company_id), "days": days},
    )
    return [
        CompanyDayOut(date=str(r.day), active_seconds=int(r.active or 0),
                      idle_seconds=int(r.idle or 0), device_count=int(r.device_count or 0))
        for r in result.fetchall()
    ]


@router.get("/company/top-apps", response_model=List[AppUsageOut])
async def top_apps(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""
            SELECT ae.app_name, SUM(ae.duration) AS seconds
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
            GROUP BY ae.app_name ORDER BY seconds DESC LIMIT :limit
        """),
        {"company_id": str(admin.company_id), "days": days, "limit": limit},
    )
    return [AppUsageOut(app_name=r.app_name, seconds=int(r.seconds or 0)) for r in result.fetchall()]


@router.get("/company/top-urls", response_model=List[UrlVisitOut])
async def top_urls(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""
            SELECT substring(ae.url FROM '^https?://([^/]+)') AS host, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit' AND ae.url IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
            GROUP BY host
            HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
            ORDER BY visits DESC LIMIT :limit
        """),
        {"company_id": str(admin.company_id), "days": days, "limit": limit},
    )
    return [UrlVisitOut(url=r.host, visits=int(r.visits or 0)) for r in result.fetchall()]


@router.get("/device/{device_id}/top-apps", response_model=List[AppUsageOut])
async def device_top_apps(
    device_id: str,
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    result_d = await db.execute(select(Device).where(Device.id == device_uuid))
    device = result_d.scalar_one_or_none()
    if not device or device.company_id != admin.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    result = await db.execute(
        text("""
            SELECT ae.app_name, SUM(ae.duration) AS seconds
            FROM activity_events ae
            WHERE ae.device_id = :device_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
            GROUP BY ae.app_name ORDER BY seconds DESC LIMIT :limit
        """),
        {"device_id": str(device.id), "days": days, "limit": limit},
    )
    return [AppUsageOut(app_name=r.app_name, seconds=int(r.seconds or 0)) for r in result.fetchall()]


@router.get("/device/{device_id}/summary", response_model=List[CompanyDayOut])
async def device_summary(
    device_id: str,
    days:  int = Query(default=7, ge=1, le=90),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    result_d = await db.execute(select(Device).where(Device.id == device_uuid))
    device = result_d.scalar_one_or_none()
    if not device or device.company_id != admin.company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    result = await db.execute(
        text("""
            SELECT
                DATE(ae.timestamp) AS day,
                SUM(CASE WHEN ae.event_type NOT IN ('idle_start','lock') THEN ae.duration ELSE 0 END) AS active,
                SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle
            FROM activity_events ae
            WHERE ae.device_id = :device_id
              AND ae.timestamp >= NOW() - make_interval(days => :days)
            GROUP BY day ORDER BY day DESC
        """),
        {"device_id": str(device.id), "days": days},
    )
    return [
        CompanyDayOut(date=str(r.day), active_seconds=int(r.active or 0),
                      idle_seconds=int(r.idle or 0), device_count=1)
        for r in result.fetchall()
    ]
