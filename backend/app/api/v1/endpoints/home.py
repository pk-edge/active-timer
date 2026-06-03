"""Home-section dashboards: Organization Overview, Team Management, Activity
Dashboard. All endpoints are group-scoped (an RM sees only their groups) via the
shared scoping engine, and accept an optional ?group_id= filter.

Note: this product has no app/URL *productivity classification*, so these
dashboards report ACTIVITY (active time, activity status, app/site usage by time)
rather than ActivTrak-style productive/unproductive percentages.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.core.config import get_settings
from app.core.scoping import visible_device_ids, ALL
from app.db.models import Device, Employee, GroupMember, Group

router = APIRouter()
settings = get_settings()

# Events that mean the user stepped away (vs. actively working).
_PASSIVE_EVENTS = ("lock", "idle_start")


async def _group_device_ids(group_id: str, company_id, db: AsyncSession) -> set:
    try:
        gid = uuid.UUID(group_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid group id")
    exists = await db.execute(select(Group.id).where(Group.id == gid, Group.company_id == company_id))
    if not exists.first():
        raise HTTPException(status_code=404, detail="Group not found")
    direct = await db.execute(
        select(GroupMember.device_id).where(
            GroupMember.group_id == gid, GroupMember.member_type == "device")
    )
    via_emp = await db.execute(
        text("""
            SELECT d.id FROM devices d
            JOIN group_members gm ON gm.employee_id = d.employee_id
            WHERE gm.group_id = :gid AND gm.member_type = 'employee'
        """),
        {"gid": str(gid)},
    )
    return {r[0] for r in direct.all() if r[0]} | {r[0] for r in via_emp.all()}


async def _resolve_ids(user: Employee, group_id: str | None, db: AsyncSession):
    """Return ALL or an explicit list of device ids = scope ∩ optional group."""
    vids = await visible_device_ids(user, db)
    if not group_id:
        return vids
    gset = await _group_device_ids(group_id, user.company_id, db)
    if vids is ALL:
        return list(gset)
    return list(set(vids) & gset)


def _clause(ids, col: str = "d.id"):
    """Build (sql_fragment, params) restricting col to ids; empty for ALL."""
    if ids is ALL:
        return "", {}
    return f" AND {col} = ANY(:hids)", {"hids": [str(i) for i in ids]}


async def _status_rows(ids, company_id, db: AsyncSession) -> list[dict]:
    frag, params = _clause(ids, "d.id")
    params["company_id"] = str(company_id)
    rows = await db.execute(
        text(f"""
            SELECT d.id, d.hostname, d.platform, d.last_seen,
                   e.name AS employee_name,
                   le.event_type AS latest_type,
                   la.app_name   AS latest_app,
                   EXTRACT(EPOCH FROM d.last_seen)::bigint AS last_seen_unix
            FROM devices d
            LEFT JOIN employees e ON e.id = d.employee_id
            LEFT JOIN LATERAL (
                SELECT event_type FROM activity_events ae
                WHERE ae.device_id = d.id ORDER BY ae.timestamp DESC LIMIT 1
            ) le ON TRUE
            LEFT JOIN LATERAL (
                SELECT app_name FROM activity_events ae
                WHERE ae.device_id = d.id AND ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL ORDER BY ae.timestamp DESC LIMIT 1
            ) la ON TRUE
            WHERE d.company_id = :company_id AND d.is_active = TRUE{frag}
            ORDER BY d.last_seen DESC NULLS LAST
        """),
        params,
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)
    out = []
    for r in rows.fetchall():
        if r.last_seen is None or r.last_seen < cutoff:
            st = "inactive"
        elif r.latest_type in _PASSIVE_EVENTS:
            st = "passive"
        else:
            st = "active"
        out.append({
            "device_id": str(r.id),
            "hostname": r.hostname,
            "platform": r.platform,
            "employee_name": r.employee_name,
            "status": st,
            "current_app": r.latest_app if st == "active" else None,
            "last_seen_unix": int(r.last_seen_unix) if r.last_seen_unix else None,
        })
    return out


@router.get("/overview")
async def overview(
    group_id: str | None = Query(default=None),
    days:     int = Query(default=7, ge=1, le=90),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return {"total_devices": 0, "active_today": 0,
                "status": {"active": 0, "passive": 0, "inactive": 0},
                "avg_active_seconds_today": 0, "trend": []}

    rows = await _status_rows(ids, user.company_id, db)
    status_counts = {"active": 0, "passive": 0, "inactive": 0}
    for r in rows:
        status_counts[r["status"]] += 1

    frag, params = _clause(ids, "ae.device_id")
    params["company_id"] = str(user.company_id)
    params["days"] = days

    # Active devices today + avg active seconds today.
    today = await db.execute(
        text(f"""
            SELECT ae.device_id,
                   SUM(CASE WHEN ae.event_type NOT IN ('idle_start','lock') THEN ae.duration ELSE 0 END) AS active
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.timestamp >= date_trunc('day', NOW()){frag}
            GROUP BY ae.device_id
        """),
        params,
    )
    today_rows = today.fetchall()
    active_today = len(today_rows)
    total_active_secs = sum(int(r.active or 0) for r in today_rows)
    avg_active = int(total_active_secs / active_today) if active_today else 0

    # Daily active-seconds trend.
    trend = await db.execute(
        text(f"""
            SELECT DATE(ae.timestamp) AS day,
                   SUM(CASE WHEN ae.event_type NOT IN ('idle_start','lock') THEN ae.duration ELSE 0 END) AS active
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}
            GROUP BY day ORDER BY day ASC
        """),
        params,
    )
    trend_out = [{"date": str(r.day), "active_seconds": int(r.active or 0)} for r in trend.fetchall()]

    return {
        "total_devices": len(rows),
        "active_today": active_today,
        "status": status_counts,
        "avg_active_seconds_today": avg_active,
        "trend": trend_out,
    }


@router.get("/realtime")
async def realtime(
    group_id: str | None = Query(default=None),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []
    return await _status_rows(ids, user.company_id, db)


@router.get("/app-usage")
async def app_usage(
    group_id: str | None = Query(default=None),
    days:     int = Query(default=1, ge=1, le=90),
    limit:    int = Query(default=12, ge=1, le=50),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []
    frag, params = _clause(ids, "ae.device_id")
    params.update({"company_id": str(user.company_id), "days": days, "limit": limit})
    res = await db.execute(
        text(f"""
            SELECT ae.app_name, SUM(ae.duration) AS seconds
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change' AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}
            GROUP BY ae.app_name ORDER BY seconds DESC LIMIT :limit
        """),
        params,
    )
    return [{"app_name": r.app_name, "seconds": int(r.seconds or 0)} for r in res.fetchall()]


@router.get("/web-usage")
async def web_usage(
    group_id: str | None = Query(default=None),
    days:     int = Query(default=1, ge=1, le=90),
    limit:    int = Query(default=12, ge=1, le=50),
    user: Employee = Depends(require("home", "view")),
    db: AsyncSession = Depends(get_db),
):
    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []
    frag, params = _clause(ids, "ae.device_id")
    params.update({"company_id": str(user.company_id), "days": days, "limit": limit})
    res = await db.execute(
        text(f"""
            SELECT substring(ae.url FROM '^https?://([^/]+)') AS host, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit' AND ae.url IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days){frag}
            GROUP BY host HAVING substring(ae.url FROM '^https?://([^/]+)') IS NOT NULL
            ORDER BY visits DESC LIMIT :limit
        """),
        params,
    )
    return [{"host": r.host, "visits": int(r.visits or 0)} for r in res.fetchall()]
