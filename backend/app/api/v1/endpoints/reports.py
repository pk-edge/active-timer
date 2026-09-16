import asyncio
import uuid
import json
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.api.deps import get_db, get_current_admin, require
from app.core.scoping import visible_device_ids, ALL
from app.core.RedisCache import cache_get, cache_set, FRESH_ANALYTICS, STALE_ANALYTICS, FRESH_DETAIL, STALE_DETAIL
from app.core.RedisClient import get_client
from app.db.base import AsyncSessionLocal
from app.db.models import Device, Employee
from app.schemas.report import CompanyDayOut, AppUsageOut, UrlVisitOut

router = APIRouter()

# OS-level idle/lock-screen process names that must never count as active work.
# These appear as foreground-window events on Windows/macOS when the screen is
# idle or locked, inflating active time and polluting the top-apps list.
_IDLE_APPS = (
    "System Idle Process", "LockApp", "LogonUI",
    "Idle", "screensaver",
)
# SQL fragment for active-time CASE expressions (window_change only, real apps)
_ACTIVE_CASE = """CASE
    WHEN ae.event_type = 'window_change'
     AND ae.app_name IS NOT NULL
     AND ae.app_name NOT IN ('System Idle Process','LockApp','LogonUI','Idle','screensaver')
    THEN ae.duration ELSE 0
END"""
# SQL fragment to exclude idle app names from top-app lists
_EXCL_IDLE = (
    "AND ae.app_name NOT IN "
    "('System Idle Process','LockApp','LogonUI','Idle','screensaver')"
)


async def _scope_clause(admin: Employee, db: AsyncSession) -> tuple[str, dict]:
    """Return an SQL fragment + params restricting ae.device_id to the user's scope.

    Empty for scope='all'. For scoped users, an empty id list yields no rows
    (ANY('{}') matches nothing), which correctly shows an RM-with-no-groups nothing.
    """
    ids = await visible_device_ids(admin, db)
    if ids is ALL:
        return "", {}
    return " AND ae.device_id = ANY(:vids)", {"vids": [str(i) for i in ids]}


async def _get_visible_device(device_id: str, admin: Employee, db: AsyncSession) -> Device:
    """Fetch a device by id, enforcing company + visibility scope in one pass."""
    try:
        device_uuid = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid device id")

    ids = await visible_device_ids(admin, db)
    q = select(Device).where(
        Device.id == device_uuid,
        Device.company_id == admin.company_id,
        Device.is_deleted == False,  # noqa: E712
    )
    if ids is not ALL:
        q = q.where(Device.id.in_(ids))
    device = (await db.execute(q)).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return device


@router.get("/company/summary", response_model=List[CompanyDayOut])
async def company_summary(
    days:  int = Query(default=7, ge=1, le=90),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    clause, sparams = await _scope_clause(admin, db)

    # Hoist scalars off the ORM object BEFORE any cache helper runs: a helper
    # error triggers db.rollback(), which expires `admin`, and a later
    # admin.company_id access would raise MissingGreenlet (500).
    cid = str(admin.company_id)

    _scope_fp = "all" if not clause else str(admin.id)
    _ckey = ("report:summary", cid, _scope_fp, days)
    _redis_hit, _ = await cache_get(_ckey)
    if _redis_hit is not None:
        return _redis_hit

    # Fast path: full-scope admins read from the pre-aggregated cache.
    # Scoped users (own/groups) fall through to the live query — their result
    # is a device-specific subset the cache does not store.
    if not clause:
        from app.core import report_cache
        cached = await report_cache.summary_from_cache(db, cid, days)
        if cached is not None:
            _items = [
                CompanyDayOut(date=str(r.day), active_seconds=r.active_seconds,
                              idle_seconds=r.idle_seconds, device_count=r.device_count)
                for r in cached
            ]
            await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _items

    # Fallback: live aggregation (scoped users or cache miss/stale).
    result = await db.execute(
        text(f"""
            SELECT
                DATE(ae.timestamp) AS day,
                SUM({_ACTIVE_CASE}) AS active,
                SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle,
                COUNT(DISTINCT ae.device_id) AS device_count
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.timestamp >= NOW() - make_interval(days => :days){clause}
            GROUP BY day ORDER BY day DESC
        """),
        {"company_id": cid, "days": days, **sparams},
    )
    _items = [
        CompanyDayOut(date=str(r.day), active_seconds=int(r.active or 0),
                      idle_seconds=int(r.idle or 0), device_count=int(r.device_count or 0))
        for r in result.fetchall()
    ]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _items


@router.get("/company/top-apps", response_model=List[AppUsageOut])
async def top_apps(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    clause, sparams = await _scope_clause(admin, db)

    cid = str(admin.company_id)  # hoisted: see company_summary

    _scope_fp = "all" if not clause else str(admin.id)
    _ckey = ("report:top_apps", cid, _scope_fp, days, limit)
    _redis_hit, _ = await cache_get(_ckey)
    if _redis_hit is not None:
        return _redis_hit

    # Fast path: full-scope admins + standard window (7 / 14 / 30) read from cache.
    if not clause:
        from app.core import report_cache
        cached = await report_cache.top_apps_from_cache(db, cid, days, limit)
        if cached is not None:
            _items = [AppUsageOut(app_name=r.app_name, seconds=r.seconds) for r in cached]
            await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _items

    # Fallback: live aggregation.
    result = await db.execute(
        text(f"""
            SELECT normalize_app_name(ae.app_name) AS app_name,
                   SUM(ae.duration) AS seconds
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
              {_EXCL_IDLE}{clause}
            GROUP BY normalize_app_name(ae.app_name) ORDER BY seconds DESC LIMIT :limit
        """),
        {"company_id": cid, "days": days, "limit": limit, **sparams},
    )
    _items = [AppUsageOut(app_name=r.app_name, seconds=int(r.seconds or 0)) for r in result.fetchall()]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _items


@router.get("/company/top-urls", response_model=List[UrlVisitOut])
async def top_urls(
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    clause, sparams = await _scope_clause(admin, db)

    cid = str(admin.company_id)  # hoisted: see company_summary

    _scope_fp = "all" if not clause else str(admin.id)
    _ckey = ("report:top_urls", cid, _scope_fp, days, limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    result = await db.execute(
        text(f"""
            SELECT substring(ae.url FROM '^https?://([^/]+)') AS host, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit' AND ae.url IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){clause}
            GROUP BY host
            HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
            ORDER BY visits DESC LIMIT :limit
        """),
        {"company_id": cid, "days": days, "limit": limit, **sparams},
    )
    _items = [UrlVisitOut(url=r.host, visits=int(r.visits or 0)) for r in result.fetchall()]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _items


@router.get("/device/{device_id}/top-apps", response_model=List[AppUsageOut])
async def device_top_apps(
    device_id: str,
    days:  int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_visible_device(device_id, admin, db)

    _ckey = ("report:dev_top_apps", str(device.id), days, limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    result = await db.execute(
        text("""
            SELECT normalize_app_name(ae.app_name) AS app_name,
                   SUM(ae.duration) AS seconds
            FROM activity_events ae
            WHERE ae.device_id = :device_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
              AND ae.app_name NOT IN ('System Idle Process','LockApp','LogonUI','Idle','screensaver')
            GROUP BY normalize_app_name(ae.app_name) ORDER BY seconds DESC LIMIT :limit
        """),
        {"device_id": str(device.id), "days": days, "limit": limit},
    )
    _items = [AppUsageOut(app_name=r.app_name, seconds=int(r.seconds or 0)) for r in result.fetchall()]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
    return _items


@router.get("/device/{device_id}/summary", response_model=List[CompanyDayOut])
async def device_summary(
    device_id: str,
    days:  int = Query(default=7, ge=1, le=90),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_visible_device(device_id, admin, db)

    _ckey = ("report:dev_summary", str(device.id), days)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    result = await db.execute(
        text(f"""
            SELECT
                DATE(ae.timestamp) AS day,
                SUM({_ACTIVE_CASE}) AS active,
                SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle
            FROM activity_events ae
            WHERE ae.device_id = :device_id
              AND ae.timestamp >= NOW() - make_interval(days => :days)
            GROUP BY day ORDER BY day DESC
        """),
        {"device_id": str(device.id), "days": days},
    )
    _items = [
        CompanyDayOut(date=str(r.day), active_seconds=int(r.active or 0),
                      idle_seconds=int(r.idle or 0), device_count=1)
        for r in result.fetchall()
    ]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
    return _items


# ── Fleet report schemas ───────────────────────────────────────────────────────

class PlatformCount(BaseModel):
    platform: str
    count: int

class TopDeviceOut(BaseModel):
    device_id: str
    hostname: str
    employee_name: Optional[str]
    active_seconds: int

class FleetReportOut(BaseModel):
    since: str
    until: str
    total_devices: int
    online_devices: int
    offline_devices: int
    archived_devices: int
    platform_breakdown: List[PlatformCount]
    daily_activity: List[CompanyDayOut]
    top_apps: List[AppUsageOut]
    most_active_devices: List[TopDeviceOut]


# ── Device full-report schemas ─────────────────────────────────────────────────

class DayBreakdownOut(BaseModel):
    date: str
    active_seconds: int
    idle_seconds: int

class AppDetailOut(BaseModel):
    app_name: str
    seconds: int
    pct: float

class DeviceInfoOut(BaseModel):
    id: str
    hostname: str
    platform: str
    employee_name: Optional[str]
    enrolled_at: str
    last_seen: Optional[str]

class DeviceFullReportOut(BaseModel):
    device: DeviceInfoOut
    since: str
    until: str
    total_active_seconds: int
    total_idle_seconds: int
    working_days: int
    daily_breakdown: List[DayBreakdownOut]
    top_apps: List[AppDetailOut]
    top_urls: List[UrlVisitOut]


# ── Device roster schemas (registered-device inventory, no activity metrics) ──

class DeviceRosterRowOut(BaseModel):
    hostname: str
    platform: str                    # normalized label: Windows / macOS / Linux / Other
    raw_platform: str                # as reported by the agent: win32 / darwin / …
    employee_name: Optional[str]
    status: str                      # online | offline | archived
    agent_version: Optional[str]
    enrolled_at: str
    last_seen: Optional[str]

class DeviceRosterReportOut(BaseModel):
    generated_at: str
    total_devices: int
    online_devices: int
    offline_devices: int
    archived_devices: int
    unassigned_devices: int
    platform_breakdown: List[PlatformCount]
    devices: List[DeviceRosterRowOut]


# ── Report job queue (Redis-backed background jobs) ───────────────────────────

_JOB_TTL = 1800  # 30 minutes


class ReportJobRequest(BaseModel):
    type: str                          # "device_full" | "fleet" | "device_roster"
    device_id: Optional[str] = None
    since: Optional[str] = None        # YYYY-MM-DD — required except for device_roster
    until: Optional[str] = None        # YYYY-MM-DD (inclusive) — same


class ReportJobOut(BaseModel):
    job_id: str
    type: str
    status: str                 # pending | running | done | failed
    progress: int               # 0-100
    error: Optional[str] = None
    result: Optional[dict] = None


async def _job_get(job_id: str) -> dict | None:
    r = get_client()
    if not r:
        return None
    raw = await r.get(f"tt:rjob:{job_id}")
    return json.loads(raw) if raw else None


async def _job_set(job_id: str, data: dict) -> None:
    r = get_client()
    if r:
        await r.setex(f"tt:rjob:{job_id}", _JOB_TTL, json.dumps(data))


# ── Background workers ────────────────────────────────────────────────────────

async def _run_device_full_job(
    job_id: str, device_id: str, company_id: str, since: str, until: str,
) -> None:
    """Run a device_full report in the background; update job state in Redis throughout."""

    async def _upd(st: str, prog: int, error: str | None = None, result: dict | None = None):
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_full",
            "status": st, "progress": prog, "error": error, "result": result,
        })

    try:
        await _upd("running", 5)
        since_dt = date.fromisoformat(since)
        until_dt = date.fromisoformat(until)

        async with AsyncSessionLocal() as db:
            dev_row = (await db.execute(text("""
                SELECT d.hostname, d.platform, d.enrolled_at, d.last_seen,
                       e.name AS employee_name
                FROM   devices d
                LEFT JOIN employees e ON e.id = d.employee_id
                WHERE  d.id = :did AND d.company_id = :cid AND d.is_deleted = FALSE
            """), {"did": device_id, "cid": company_id})).fetchone()

            if not dev_row:
                await _upd("failed", 0, error="Device not found")
                return

            await _upd("running", 20)

            daily = (await db.execute(text(f"""
                SELECT DATE(ae.timestamp) AS day,
                       SUM({_ACTIVE_CASE}) AS active,
                       SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle
                FROM   activity_events ae
                WHERE  ae.device_id = :did
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                GROUP  BY day ORDER BY day
            """), {"did": device_id, "since": since_dt, "until": until_dt})).fetchall()

            await _upd("running", 50)

            apps_raw = (await db.execute(text("""
                SELECT normalize_app_name(ae.app_name) AS app_name,
                       SUM(ae.duration) AS seconds
                FROM   activity_events ae
                WHERE  ae.device_id = :did
                  AND  ae.event_type = 'window_change'
                  AND  ae.app_name IS NOT NULL
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                  AND  ae.app_name NOT IN ('System Idle Process','LockApp','LogonUI','Idle','screensaver')
                GROUP  BY normalize_app_name(ae.app_name)
                ORDER  BY seconds DESC LIMIT 20
            """), {"did": device_id, "since": since_dt, "until": until_dt})).fetchall()

            await _upd("running", 75)

            top_urls = (await db.execute(text("""
                SELECT substring(ae.url FROM '^https?://([^/]+)') AS host,
                       COUNT(*) AS visits
                FROM   activity_events ae
                WHERE  ae.device_id = :did
                  AND  ae.event_type = 'url_visit'
                  AND  ae.url IS NOT NULL
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                GROUP  BY host
                HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
                ORDER  BY visits DESC LIMIT 15
            """), {"did": device_id, "since": since_dt, "until": until_dt})).fetchall()

            await _upd("running", 90)

            total_active   = sum(int(r.active or 0) for r in daily)
            total_idle     = sum(int(r.idle   or 0) for r in daily)
            working_days   = len([r for r in daily if int(r.active or 0) > 0])
            total_app_secs = sum(int(r.seconds or 0) for r in apps_raw) or 1

            enrolled_str  = dev_row.enrolled_at.strftime("%Y-%m-%d") if dev_row.enrolled_at else ""
            last_seen_str = dev_row.last_seen.strftime("%Y-%m-%d %H:%M UTC") if dev_row.last_seen else None

            result = {
                "device": {
                    "id": device_id,
                    "hostname": dev_row.hostname,
                    "platform": (dev_row.platform or "").capitalize(),
                    "employee_name": dev_row.employee_name,
                    "enrolled_at": enrolled_str,
                    "last_seen": last_seen_str,
                },
                "since": str(since_dt),
                "until": str(until_dt),
                "total_active_seconds": total_active,
                "total_idle_seconds": total_idle,
                "working_days": working_days,
                "daily_breakdown": [
                    {"date": str(r.day), "active_seconds": int(r.active or 0), "idle_seconds": int(r.idle or 0)}
                    for r in daily
                ],
                "top_apps": [
                    {
                        "app_name": r.app_name,
                        "seconds": int(r.seconds or 0),
                        "pct": round(int(r.seconds or 0) / total_app_secs * 100, 1),
                    }
                    for r in apps_raw
                ],
                "top_urls": [{"url": r.host, "visits": int(r.visits or 0)} for r in top_urls],
            }

            # Warm the regular GET cache so an immediate follow-up request is free
            _ckey = ("report:dev_full", device_id, str(since_dt), str(until_dt))
            await cache_set(_ckey, result, fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)

            await _upd("done", 100, result=result)

    except Exception as exc:  # noqa: BLE001
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_full",
            "status": "failed", "progress": 0, "error": str(exc), "result": None,
        })


async def _run_fleet_job(
    job_id: str, company_id: str, since: str, until: str,
) -> None:
    """Run a fleet report in the background.

    All five queries run concurrently (asyncio.gather + separate sessions) so
    wall time ≈ slowest single query instead of the sum of all five.
    """

    async def _upd(st: str, prog: int, error: str | None = None, result: dict | None = None):
        await _job_set(job_id, {
            "job_id": job_id, "type": "fleet",
            "status": st, "progress": prog, "error": error, "result": result,
        })

    try:
        await _upd("running", 5)
        since_dt = date.fromisoformat(since)
        until_dt = date.fromisoformat(until)

        p_meta = {"cid": company_id, "cutoff": ONLINE_CUTOFF_SECS}
        p_time = {"cid": company_id, "since": since_dt, "until": until_dt}

        # Each helper opens its own session so they can truly run in parallel.

        async def q_counts():
            async with AsyncSessionLocal() as db:
                return (await db.execute(text("""
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE is_archived = TRUE) AS archived,
                        COUNT(*) FILTER (
                            WHERE is_archived = FALSE
                              AND last_seen >= NOW() - (:cutoff * INTERVAL '1 second')
                        ) AS online,
                        COUNT(*) FILTER (
                            WHERE is_archived = FALSE
                              AND (last_seen IS NULL
                                   OR last_seen < NOW() - (:cutoff * INTERVAL '1 second'))
                        ) AS offline
                    FROM devices
                    WHERE company_id = :cid AND is_deleted = FALSE
                """), p_meta)).fetchone()

        async def q_platforms():
            async with AsyncSessionLocal() as db:
                return (await db.execute(text("""
                    SELECT platform, COUNT(*) AS cnt
                    FROM   devices
                    WHERE  company_id = :cid AND is_deleted = FALSE AND is_archived = FALSE
                    GROUP  BY platform ORDER BY cnt DESC
                """), p_meta)).fetchall()

        async def q_daily():
            async with AsyncSessionLocal() as db:
                return (await db.execute(text(f"""
                    SELECT DATE(ae.timestamp) AS day,
                           SUM({_ACTIVE_CASE}) AS active,
                           SUM(CASE WHEN ae.event_type = 'idle_start'
                               THEN ae.duration ELSE 0 END) AS idle,
                           COUNT(DISTINCT ae.device_id) AS device_count
                    FROM   activity_events ae
                    JOIN   devices d ON d.id = ae.device_id
                    WHERE  d.company_id = :cid
                      AND  ae.timestamp >= CAST(:since AS date)
                      AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                    GROUP  BY day ORDER BY day
                """), p_time)).fetchall()

        async def q_apps():
            async with AsyncSessionLocal() as db:
                return (await db.execute(text(f"""
                    SELECT normalize_app_name(ae.app_name) AS app_name,
                           SUM(ae.duration) AS seconds
                    FROM   activity_events ae
                    JOIN   devices d ON d.id = ae.device_id
                    WHERE  d.company_id = :cid
                      AND  ae.event_type = 'window_change'
                      AND  ae.app_name IS NOT NULL
                      AND  ae.timestamp >= CAST(:since AS date)
                      AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                      {_EXCL_IDLE}
                    GROUP  BY normalize_app_name(ae.app_name)
                    ORDER  BY seconds DESC LIMIT 20
                """), p_time)).fetchall()

        async def q_top_devices():
            async with AsyncSessionLocal() as db:
                return (await db.execute(text(f"""
                    SELECT d.id::text AS device_id,
                           d.hostname,
                           e.name     AS employee_name,
                           SUM({_ACTIVE_CASE}) AS active_seconds
                    FROM   activity_events ae
                    JOIN   devices   d ON d.id = ae.device_id
                    LEFT JOIN employees e ON e.id = d.employee_id
                    WHERE  d.company_id = :cid
                      AND  ae.timestamp >= CAST(:since AS date)
                      AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                    GROUP  BY d.id, d.hostname, e.name
                    ORDER  BY active_seconds DESC LIMIT 10
                """), p_time)).fetchall()

        # Fire all five concurrently — wall time ≈ max(query times) not sum
        counts, platforms, daily, top_apps, top_devices = await asyncio.gather(
            q_counts(), q_platforms(), q_daily(), q_apps(), q_top_devices(),
        )

        await _upd("running", 90)

        total_app_secs = sum(int(r.seconds or 0) for r in top_apps)
        result = {
            "since": str(since_dt),
            "until": str(until_dt),
            "total_devices":    int(counts.total    or 0),
            "online_devices":   int(counts.online   or 0),
            "offline_devices":  int(counts.offline  or 0),
            "archived_devices": int(counts.archived or 0),
            "platform_breakdown": [
                {"platform": (r.platform or "").capitalize(), "count": int(r.cnt)}
                for r in platforms
            ],
            "daily_activity": [
                {
                    "date":           str(r.day),
                    "active_seconds": int(r.active        or 0),
                    "idle_seconds":   int(r.idle          or 0),
                    "device_count":   int(r.device_count  or 0),
                }
                for r in daily
            ],
            "top_apps": [
                {
                    "app_name": r.app_name,
                    "seconds":  int(r.seconds or 0),
                    "pct": round(int(r.seconds or 0) / total_app_secs * 100, 1)
                           if total_app_secs else 0,
                }
                for r in top_apps
            ],
            "most_active_devices": [
                {
                    "device_id":      r.device_id,
                    "hostname":       r.hostname,
                    "employee_name":  r.employee_name,
                    "active_seconds": int(r.active_seconds or 0),
                }
                for r in top_devices
            ],
        }

        _ckey = ("report:fleet", company_id, str(since_dt), str(until_dt))
        await cache_set(_ckey, result, fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
        await _upd("done", 100, result=result)

    except Exception as exc:  # noqa: BLE001
        await _job_set(job_id, {
            "job_id": job_id, "type": "fleet",
            "status": "failed", "progress": 0, "error": str(exc), "result": None,
        })


# ── Device roster worker ──────────────────────────────────────────────────────

# The agent reports sys.platform, so Windows arrives as 'win32' and macOS as
# 'darwin'. Anything not listed rolls up into "Other" rather than leaking a raw
# platform string into the report's grouping.
_PLATFORM_LABELS = {
    "win32": "Windows", "windows": "Windows", "cygwin": "Windows",
    "darwin": "macOS", "macos": "macOS",
    "linux": "Linux", "linux2": "Linux",
}


def _platform_label(raw: str | None) -> str:
    return _PLATFORM_LABELS.get((raw or "").strip().lower(), "Other")


async def _fetch_roster_rows(db: AsyncSession, company_id: str, device_ids: list[str] | None):
    """Every registered (non-deleted) device in scope, archived ones included.

    `device_ids=None` means company-wide (scope 'all'); a list restricts to the
    caller's visible devices as resolved by app/core/scoping.py.
    """
    scope = "" if device_ids is None else " AND d.id = ANY(:vids)"
    params: dict = {"cid": company_id, "cutoff": ONLINE_CUTOFF_SECS}
    if device_ids is not None:
        params["vids"] = device_ids

    return (await db.execute(text(f"""
        SELECT d.hostname,
               d.platform,
               d.agent_version,
               d.enrolled_at,
               d.last_seen,
               d.is_archived,
               COALESCE(
                   d.is_archived = FALSE
                   AND d.last_seen >= NOW() - (:cutoff * INTERVAL '1 second'),
                   FALSE
               ) AS is_online,
               e.name AS employee_name
        FROM   devices d
        LEFT JOIN employees e ON e.id = d.employee_id
        WHERE  d.company_id = :cid
          AND  d.is_deleted = FALSE{scope}
        ORDER  BY d.is_archived, e.name NULLS LAST, d.hostname
    """), params)).fetchall()


def _roster_result(rows) -> dict:
    """Shape roster rows into the report payload: inventory + counts, no activity."""
    devices: list[dict] = []
    platform_counts: dict[str, int] = {}
    online = offline = archived = unassigned = 0

    for r in rows:
        label = _platform_label(r.platform)
        platform_counts[label] = platform_counts.get(label, 0) + 1

        if r.is_archived:
            row_status = "archived"
            archived += 1
        elif r.is_online:
            row_status = "online"
            online += 1
        else:
            row_status = "offline"
            offline += 1

        if not r.employee_name:
            unassigned += 1

        devices.append({
            "hostname":      r.hostname,
            "platform":      label,
            "raw_platform":  r.platform or "",
            "employee_name": r.employee_name,
            "status":        row_status,
            "agent_version": r.agent_version,
            "enrolled_at":   r.enrolled_at.isoformat() if r.enrolled_at else "",
            "last_seen":     r.last_seen.isoformat() if r.last_seen else None,
        })

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "total_devices":      len(devices),
        "online_devices":     online,
        "offline_devices":    offline,
        "archived_devices":   archived,
        "unassigned_devices": unassigned,
        "platform_breakdown": [
            {"platform": p, "count": c}
            for p, c in sorted(platform_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "devices": devices,
    }


async def _run_device_roster_job(
    job_id: str, company_id: str, device_ids: list[str] | None,
) -> None:
    """Registered-device inventory snapshot. One cheap query — no activity_events."""

    async def _upd(st: str, prog: int, error: str | None = None, result: dict | None = None):
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_roster",
            "status": st, "progress": prog, "error": error, "result": result,
        })

    try:
        await _upd("running", 10)
        async with AsyncSessionLocal() as db:
            rows = await _fetch_roster_rows(db, company_id, device_ids)
        await _upd("done", 100, result=_roster_result(rows))

    except Exception as exc:  # noqa: BLE001
        await _job_set(job_id, {
            "job_id": job_id, "type": "device_roster",
            "status": "failed", "progress": 0, "error": str(exc), "result": None,
        })


# ── Job endpoints ─────────────────────────────────────────────────────────────

@router.post("/jobs", response_model=ReportJobOut, status_code=202)
async def create_report_job(
    req: ReportJobRequest,
    background_tasks: BackgroundTasks,
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Queue a report job. Returns job_id immediately; poll GET /jobs/{id} for progress.
    Falls back to running synchronously when Redis is unavailable — the response will
    contain status='done' and the full result already populated."""
    if req.type not in ("device_full", "fleet", "device_roster"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="type must be 'device_full', 'fleet' or 'device_roster'",
        )

    cid = str(admin.company_id)

    # device_roster is a point-in-time inventory snapshot — it takes no date range.
    if req.type != "device_roster":
        if not req.since or not req.until:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"since and until are required for {req.type}",
            )
        _parse_date(req.since, "since")   # validate dates early
        _parse_date(req.until, "until")

    device_id: str | None = None
    if req.type == "device_full":
        if not req.device_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="device_id required for device_full")
        device = await _get_visible_device(req.device_id, admin, db)
        device_id = str(device.id)

    # Resolve the caller's device visibility here, while the request session and
    # `admin` are alive — the background worker has neither.
    roster_ids: list[str] | None = None
    if req.type == "device_roster":
        ids = await visible_device_ids(admin, db)
        roster_ids = None if ids is ALL else [str(i) for i in ids]

    job_id = str(uuid.uuid4())

    # ── Redis available: true background job ──────────────────────────────────
    if get_client():
        initial: dict = {
            "job_id": job_id, "type": req.type,
            "status": "pending", "progress": 0, "error": None, "result": None,
        }
        await _job_set(job_id, initial)
        if req.type == "device_full":
            background_tasks.add_task(_run_device_full_job, job_id, device_id, cid, req.since, req.until)
        elif req.type == "device_roster":
            background_tasks.add_task(_run_device_roster_job, job_id, cid, roster_ids)
        else:
            background_tasks.add_task(_run_fleet_job, job_id, cid, req.since, req.until)
        return initial

    # ── Redis unavailable: run synchronously, return completed job inline ─────
    # Mirrors the background workers but uses the request's db session directly.
    try:
        if req.type == "device_roster":
            result = _roster_result(await _fetch_roster_rows(db, cid, roster_ids))

        elif req.type == "device_full":
            since_dt = date.fromisoformat(req.since)
            until_dt = date.fromisoformat(req.until)
            did = device_id

            dev_row = (await db.execute(text("""
                SELECT d.hostname, d.platform, d.enrolled_at, d.last_seen,
                       e.name AS employee_name
                FROM   devices d
                LEFT JOIN employees e ON e.id = d.employee_id
                WHERE  d.id = :did AND d.company_id = :cid AND d.is_deleted = FALSE
            """), {"did": did, "cid": cid})).fetchone()

            daily = (await db.execute(text(f"""
                SELECT DATE(ae.timestamp) AS day,
                       SUM({_ACTIVE_CASE}) AS active,
                       SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle
                FROM   activity_events ae
                WHERE  ae.device_id = :did
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                GROUP  BY day ORDER BY day
            """), {"did": did, "since": since_dt, "until": until_dt})).fetchall()

            apps_raw = (await db.execute(text("""
                SELECT normalize_app_name(ae.app_name) AS app_name,
                       SUM(ae.duration) AS seconds
                FROM   activity_events ae
                WHERE  ae.device_id = :did
                  AND  ae.event_type = 'window_change'
                  AND  ae.app_name IS NOT NULL
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                  AND  ae.app_name NOT IN ('System Idle Process','LockApp','LogonUI','Idle','screensaver')
                GROUP  BY normalize_app_name(ae.app_name)
                ORDER  BY seconds DESC LIMIT 20
            """), {"did": did, "since": since_dt, "until": until_dt})).fetchall()

            top_urls = (await db.execute(text("""
                SELECT substring(ae.url FROM '^https?://([^/]+)') AS host,
                       COUNT(*) AS visits
                FROM   activity_events ae
                WHERE  ae.device_id = :did
                  AND  ae.event_type = 'url_visit'
                  AND  ae.url IS NOT NULL
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                GROUP  BY host
                HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
                ORDER  BY visits DESC LIMIT 15
            """), {"did": did, "since": since_dt, "until": until_dt})).fetchall()

            total_active   = sum(int(r.active or 0) for r in daily)
            total_idle     = sum(int(r.idle   or 0) for r in daily)
            working_days   = len([r for r in daily if int(r.active or 0) > 0])
            total_app_secs = sum(int(r.seconds or 0) for r in apps_raw) or 1
            enrolled_str   = dev_row.enrolled_at.strftime("%Y-%m-%d") if (dev_row and dev_row.enrolled_at) else ""
            last_seen_str  = dev_row.last_seen.strftime("%Y-%m-%d %H:%M UTC") if (dev_row and dev_row.last_seen) else None

            result: dict = {
                "device": {
                    "id": did,
                    "hostname": dev_row.hostname if dev_row else did,
                    "platform": (dev_row.platform or "").capitalize() if dev_row else "",
                    "employee_name": dev_row.employee_name if dev_row else None,
                    "enrolled_at": enrolled_str,
                    "last_seen": last_seen_str,
                },
                "since": req.since, "until": req.until,
                "total_active_seconds": total_active, "total_idle_seconds": total_idle,
                "working_days": working_days,
                "daily_breakdown": [
                    {"date": str(r.day), "active_seconds": int(r.active or 0), "idle_seconds": int(r.idle or 0)}
                    for r in daily
                ],
                "top_apps": [
                    {"app_name": r.app_name, "seconds": int(r.seconds or 0),
                     "pct": round(int(r.seconds or 0) / total_app_secs * 100, 1)}
                    for r in apps_raw
                ],
                "top_urls": [{"url": r.host, "visits": int(r.visits or 0)} for r in top_urls],
            }

        else:  # fleet
            since_dt = date.fromisoformat(req.since)
            until_dt = date.fromisoformat(req.until)

            counts = (await db.execute(text("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_archived = TRUE) AS archived,
                       COUNT(*) FILTER (
                           WHERE is_archived = FALSE
                             AND last_seen >= NOW() - (:cutoff * INTERVAL '1 second')
                       ) AS online,
                       COUNT(*) FILTER (
                           WHERE is_archived = FALSE
                             AND (last_seen IS NULL OR last_seen < NOW() - (:cutoff * INTERVAL '1 second'))
                       ) AS offline
                FROM devices WHERE company_id = :cid AND is_deleted = FALSE
            """), {"cid": cid, "cutoff": ONLINE_CUTOFF_SECS})).fetchone()

            platforms = (await db.execute(text("""
                SELECT platform, COUNT(*) AS cnt FROM devices
                WHERE company_id = :cid AND is_deleted = FALSE AND is_archived = FALSE
                GROUP BY platform ORDER BY cnt DESC
            """), {"cid": cid})).fetchall()

            daily = (await db.execute(text(f"""
                SELECT DATE(ae.timestamp) AS day, SUM({_ACTIVE_CASE}) AS active,
                       SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle,
                       COUNT(DISTINCT ae.device_id) AS device_count
                FROM   activity_events ae JOIN devices d ON d.id = ae.device_id
                WHERE  d.company_id = :cid
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                GROUP  BY day ORDER BY day
            """), {"cid": cid, "since": since_dt, "until": until_dt})).fetchall()

            top_apps = (await db.execute(text(f"""
                SELECT normalize_app_name(ae.app_name) AS app_name, SUM(ae.duration) AS seconds
                FROM   activity_events ae JOIN devices d ON d.id = ae.device_id
                WHERE  d.company_id = :cid AND ae.event_type = 'window_change'
                  AND  ae.app_name IS NOT NULL
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                  {_EXCL_IDLE}
                GROUP  BY normalize_app_name(ae.app_name) ORDER BY seconds DESC LIMIT 20
            """), {"cid": cid, "since": since_dt, "until": until_dt})).fetchall()

            top_devices = (await db.execute(text(f"""
                SELECT d.id::text AS device_id, d.hostname, e.name AS employee_name,
                       SUM({_ACTIVE_CASE}) AS active_seconds
                FROM   activity_events ae JOIN devices d ON d.id = ae.device_id
                LEFT JOIN employees e ON e.id = d.employee_id
                WHERE  d.company_id = :cid
                  AND  ae.timestamp >= CAST(:since AS date)
                  AND  ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
                GROUP  BY d.id, d.hostname, e.name ORDER BY active_seconds DESC LIMIT 10
            """), {"cid": cid, "since": since_dt, "until": until_dt})).fetchall()

            result = {
                "since": req.since, "until": req.until,
                "total_devices": int(counts.total or 0),
                "online_devices": int(counts.online or 0),
                "offline_devices": int(counts.offline or 0),
                "archived_devices": int(counts.archived or 0),
                "platform_breakdown": [
                    {"platform": (r.platform or "").capitalize(), "count": int(r.cnt)} for r in platforms
                ],
                "daily_activity": [
                    {"date": str(r.day), "active_seconds": int(r.active or 0),
                     "idle_seconds": int(r.idle or 0), "device_count": int(r.device_count or 0)}
                    for r in daily
                ],
                "top_apps": [{"app_name": r.app_name, "seconds": int(r.seconds or 0)} for r in top_apps],
                "most_active_devices": [
                    {"device_id": r.device_id, "hostname": r.hostname,
                     "employee_name": r.employee_name, "active_seconds": int(r.active_seconds or 0)}
                    for r in top_devices
                ],
            }

        return {
            "job_id": job_id, "type": req.type,
            "status": "done", "progress": 100, "error": None, "result": result,
        }

    except Exception as exc:  # noqa: BLE001
        return {
            "job_id": job_id, "type": req.type,
            "status": "failed", "progress": 0, "error": str(exc), "result": None,
        }


@router.get("/jobs/{job_id}", response_model=ReportJobOut)
async def get_report_job(
    job_id: str,
    _admin: Employee = Depends(require("reports", "view")),
):
    """Poll report job status. result is populated when status='done'."""
    job = await _job_get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found or expired")
    return job


# ── Helper: parse date string safely ──────────────────────────────────────────

def _parse_date(s: str, param: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date for {param}; expected YYYY-MM-DD")


# ── Fleet report endpoint ─────────────────────────────────────────────────────

ONLINE_CUTOFF_SECS = 120  # must match devices endpoint

@router.get("/devices/fleet", response_model=FleetReportOut)
async def fleet_report(
    since: str = Query(..., description="Start date YYYY-MM-DD"),
    until: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Board-level fleet report: counts, platform split, activity trend, top apps."""
    since_dt = _parse_date(since, "since")
    until_dt = _parse_date(until, "until")
    cid = str(admin.company_id)

    _ckey = ("report:fleet", cid, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # ── Device counts ──────────────────────────────────────────────────────
    counts = (await db.execute(text("""
        SELECT
            COUNT(*)                                                   AS total,
            COUNT(*) FILTER (WHERE is_archived = TRUE)                AS archived,
            COUNT(*) FILTER (
                WHERE is_archived = FALSE
                  AND last_seen >= NOW() - (:cutoff * INTERVAL '1 second')
            )                                                          AS online,
            COUNT(*) FILTER (
                WHERE is_archived = FALSE
                  AND (last_seen IS NULL OR last_seen < NOW() - (:cutoff * INTERVAL '1 second'))
            )                                                          AS offline
        FROM devices
        WHERE company_id = :cid AND is_deleted = FALSE
    """), {"cid": cid, "cutoff": ONLINE_CUTOFF_SECS})).fetchone()

    # ── Platform breakdown ─────────────────────────────────────────────────
    platforms = (await db.execute(text("""
        SELECT platform, COUNT(*) AS cnt
        FROM   devices
        WHERE  company_id = :cid AND is_deleted = FALSE AND is_archived = FALSE
        GROUP  BY platform
        ORDER  BY cnt DESC
    """), {"cid": cid})).fetchall()

    # ── Daily activity ─────────────────────────────────────────────────────
    daily = (await db.execute(text(f"""
        SELECT
            DATE(ae.timestamp)  AS day,
            SUM({_ACTIVE_CASE}) AS active,
            SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle,
            COUNT(DISTINCT ae.device_id) AS device_count
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :cid
          AND ae.timestamp >= CAST(:since AS date)
          AND ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
        GROUP BY day ORDER BY day
    """), {"cid": cid, "since": since_dt, "until": until_dt})).fetchall()

    # ── Top apps ───────────────────────────────────────────────────────────
    top_apps = (await db.execute(text(f"""
        SELECT normalize_app_name(ae.app_name) AS app_name,
               SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :cid
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since AS date)
          AND ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
          {_EXCL_IDLE}
        GROUP BY normalize_app_name(ae.app_name)
        ORDER BY seconds DESC LIMIT 20
    """), {"cid": cid, "since": since_dt, "until": until_dt})).fetchall()

    # ── Most active devices ────────────────────────────────────────────────
    top_devices = (await db.execute(text(f"""
        SELECT
            d.id::text          AS device_id,
            d.hostname,
            e.name              AS employee_name,
            SUM({_ACTIVE_CASE}) AS active_seconds
        FROM activity_events ae
        JOIN devices   d ON d.id = ae.device_id
        LEFT JOIN employees e ON e.id = d.employee_id
        WHERE d.company_id = :cid
          AND ae.timestamp >= CAST(:since AS date)
          AND ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
        GROUP BY d.id, d.hostname, e.name
        ORDER BY active_seconds DESC
        LIMIT 10
    """), {"cid": cid, "since": since_dt, "until": until_dt})).fetchall()

    _result = FleetReportOut(
        since=str(since_dt),
        until=str(until_dt),
        total_devices=int(counts.total or 0),
        online_devices=int(counts.online or 0),
        offline_devices=int(counts.offline or 0),
        archived_devices=int(counts.archived or 0),
        platform_breakdown=[PlatformCount(platform=r.platform.capitalize(), count=int(r.cnt)) for r in platforms],
        daily_activity=[
            CompanyDayOut(date=str(r.day), active_seconds=int(r.active or 0),
                          idle_seconds=int(r.idle or 0), device_count=int(r.device_count or 0))
            for r in daily
        ],
        top_apps=[AppUsageOut(app_name=r.app_name, seconds=int(r.seconds or 0)) for r in top_apps],
        most_active_devices=[
            TopDeviceOut(device_id=r.device_id, hostname=r.hostname,
                         employee_name=r.employee_name, active_seconds=int(r.active_seconds or 0))
            for r in top_devices
        ],
    )
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


# ── Device full report endpoint ───────────────────────────────────────────────

@router.get("/device/{device_id}/full", response_model=DeviceFullReportOut)
async def device_full_report(
    device_id: str,
    since: str = Query(..., description="Start date YYYY-MM-DD"),
    until: str = Query(..., description="End date YYYY-MM-DD (inclusive)"),
    admin: Employee = Depends(require("reports", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Full per-device report: day-wise breakdown, app usage, and URL visits."""
    since_dt = _parse_date(since, "since")
    until_dt = _parse_date(until, "until")
    device = await _get_visible_device(device_id, admin, db)

    did = str(device.id)
    _ckey = ("report:dev_full", did, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # ── Daily breakdown ────────────────────────────────────────────────────
    daily = (await db.execute(text(f"""
        SELECT
            DATE(ae.timestamp) AS day,
            SUM({_ACTIVE_CASE}) AS active,
            SUM(CASE WHEN ae.event_type = 'idle_start' THEN ae.duration ELSE 0 END) AS idle
        FROM activity_events ae
        WHERE ae.device_id = :did
          AND ae.timestamp >= CAST(:since AS date)
          AND ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
        GROUP BY day ORDER BY day
    """), {"did": did, "since": since_dt, "until": until_dt})).fetchall()

    total_active = sum(int(r.active or 0) for r in daily)
    total_idle   = sum(int(r.idle   or 0) for r in daily)
    working_days = len([r for r in daily if int(r.active or 0) > 0])

    # ── Top apps ───────────────────────────────────────────────────────────
    apps_raw = (await db.execute(text("""
        SELECT normalize_app_name(ae.app_name) AS app_name,
               SUM(ae.duration) AS seconds
        FROM activity_events ae
        WHERE ae.device_id = :did
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since AS date)
          AND ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
          AND ae.app_name NOT IN ('System Idle Process','LockApp','LogonUI','Idle','screensaver')
        GROUP BY normalize_app_name(ae.app_name)
        ORDER BY seconds DESC LIMIT 20
    """), {"did": did, "since": since_dt, "until": until_dt})).fetchall()

    total_app_secs = sum(int(r.seconds or 0) for r in apps_raw) or 1
    top_apps = [
        AppDetailOut(
            app_name=r.app_name,
            seconds=int(r.seconds or 0),
            pct=round(int(r.seconds or 0) / total_app_secs * 100, 1),
        )
        for r in apps_raw
    ]

    # ── Top URLs ───────────────────────────────────────────────────────────
    top_urls = (await db.execute(text("""
        SELECT substring(ae.url FROM '^https?://([^/]+)') AS host,
               COUNT(*) AS visits
        FROM activity_events ae
        WHERE ae.device_id = :did
          AND ae.event_type = 'url_visit'
          AND ae.url IS NOT NULL
          AND ae.timestamp >= CAST(:since AS date)
          AND ae.timestamp <  CAST(:until AS date) + INTERVAL '1 day'
        GROUP BY host
        HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
        ORDER BY visits DESC LIMIT 15
    """), {"did": did, "since": since_dt, "until": until_dt})).fetchall()

    # ── Device info ────────────────────────────────────────────────────────
    emp_name = device.employee.name if device.employee else None
    enrolled_str = device.enrolled_at.strftime("%Y-%m-%d") if device.enrolled_at else ""
    last_seen_str = device.last_seen.strftime("%Y-%m-%d %H:%M UTC") if device.last_seen else None

    _result = DeviceFullReportOut(
        device=DeviceInfoOut(
            id=str(device.id),
            hostname=device.hostname,
            platform=device.platform.capitalize(),
            employee_name=emp_name,
            enrolled_at=enrolled_str,
            last_seen=last_seen_str,
        ),
        since=str(since_dt),
        until=str(until_dt),
        total_active_seconds=total_active,
        total_idle_seconds=total_idle,
        working_days=working_days,
        daily_breakdown=[
            DayBreakdownOut(date=str(r.day), active_seconds=int(r.active or 0),
                            idle_seconds=int(r.idle or 0))
            for r in daily
        ],
        top_apps=top_apps,
        top_urls=[UrlVisitOut(url=r.host, visits=int(r.visits or 0)) for r in top_urls],
    )
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
    return _result
