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

from app.api.deps import get_db, require, get_current_user, get_current_device
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
    group_id:   str | None = Query(default=None),
    days:       int = Query(default=7, ge=1, le=90),
    since_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    until_date: str | None = Query(default=None, description="YYYY-MM-DD"),
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

    # ── Date range for stat cards ─────────────────────────────────────────────
    # When since_date/until_date provided use that range; otherwise "today".
    if since_date and until_date:
        try:
            stat_since = datetime.strptime(since_date, "%Y-%m-%d").date()
            stat_until = datetime.strptime(until_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
        stat_filter = " AND ae.timestamp >= CAST(:stat_since AS date) AND ae.timestamp < (CAST(:stat_until AS date) + INTERVAL '1 day')"
        params["stat_since"] = stat_since
        params["stat_until"] = stat_until
    else:
        stat_filter = " AND ae.timestamp >= date_trunc('day', NOW())"

    # Active working time = window_change events for real user apps ONLY.
    # Excludes: System Idle Process / LockApp (Windows OS placeholders that
    # appear as foreground window while the user is actually idle), and any
    # event whose app_name is NULL/blank.
    SYSTEM_IDLE_APPS = (
        "System Idle Process", "LockApp", "LogonUI",
        "Idle", "screensaver",
    )
    placeholders = ", ".join(f":ex{i}" for i in range(len(SYSTEM_IDLE_APPS)))
    for i, name in enumerate(SYSTEM_IDLE_APPS):
        params[f"ex{i}"] = name

    stat_sql = f"""
        SELECT ae.device_id,
               SUM(CASE
                   WHEN ae.event_type = 'window_change'
                    AND ae.app_name IS NOT NULL
                    AND ae.app_name NOT IN ({placeholders})
                   THEN ae.duration ELSE 0
               END) AS active
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id{stat_filter}{frag}
        GROUP BY ae.device_id
    """
    stat_result = await db.execute(text(stat_sql), params)
    stat_rows = stat_result.fetchall()
    active_today = len([r for r in stat_rows if (r.active or 0) > 0])
    total_active_secs = sum(int(r.active or 0) for r in stat_rows)
    avg_active = int(total_active_secs / active_today) if active_today else 0

    # ── Trend — daily active seconds (always keyed by `days` param) ──────────
    params["days"] = days
    trend = await db.execute(
        text(f"""
            SELECT DATE(ae.timestamp) AS day,
                   SUM(CASE
                       WHEN ae.event_type = 'window_change'
                        AND ae.app_name IS NOT NULL
                        AND ae.app_name NOT IN ({placeholders})
                       THEN ae.duration ELSE 0
                   END) AS active
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
    """
    Returns live device status.

    When the in-process ingest queue has warm cache entries for devices,
    we merge them into the SQL result so the UI sees the very latest
    app/event without waiting for the async DB write to land.
    The DB query still runs (for hostname, employee, online/offline
    logic) but it's enriched with zero-latency cache data.
    """
    from app.core.ingest_queue import ingest_queue

    ids = await _resolve_ids(user, group_id, db)
    if ids is not ALL and not ids:
        return []

    rows = await _status_rows(ids, user.company_id, db)

    # Overlay cache: if the ingest queue has a more recent state for a device,
    # update current_app / status without an extra DB round-trip.
    cache = ingest_queue.device_cache
    now   = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)

    for row in rows:
        did = row["device_id"]
        cached = cache.get(did)
        if not cached:
            continue
        cached_ts = cached.get("last_seen")
        if cached_ts and cached_ts > cutoff:
            # Device is definitely online — use cached app state
            if cached.get("current_app"):
                row["current_app"] = cached["current_app"]
            row["status"] = (
                "passive" if cached.get("latest_type") in _PASSIVE_EVENTS
                else "active"
            )

    return rows


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


@router.get("/my-stats")
async def my_stats(
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Personal stats for the logged-in user — scoped to their own devices."""
    device_rows = await db.execute(
        select(Device.id).where(Device.employee_id == user.id, Device.is_active == True)  # noqa: E712
    )
    device_ids = [str(r[0]) for r in device_rows.all()]

    if not device_ids:
        return {"active_seconds_today": 0, "top_apps": [], "recent_events": [], "device_count": 0}

    active_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(
                CASE WHEN event_type NOT IN ('idle_start','lock') THEN duration ELSE 0 END
            ), 0) AS active
            FROM activity_events
            WHERE device_id = ANY(:ids) AND timestamp >= date_trunc('day', NOW())
        """),
        {"ids": device_ids},
    )
    active_seconds = int(active_result.scalar() or 0)

    apps_result = await db.execute(
        text("""
            SELECT app_name, SUM(duration) AS seconds
            FROM activity_events
            WHERE device_id = ANY(:ids)
              AND event_type = 'window_change' AND app_name IS NOT NULL
              AND timestamp >= date_trunc('day', NOW())
            GROUP BY app_name ORDER BY seconds DESC LIMIT 5
        """),
        {"ids": device_ids},
    )
    top_apps = [{"app_name": r.app_name, "seconds": int(r.seconds or 0)} for r in apps_result.fetchall()]

    events_result = await db.execute(
        text("""
            SELECT event_type, app_name, window_title, url, duration,
                   EXTRACT(EPOCH FROM timestamp)::bigint AS ts_unix
            FROM activity_events WHERE device_id = ANY(:ids)
            ORDER BY timestamp DESC LIMIT 10
        """),
        {"ids": device_ids},
    )
    recent_events = [
        {
            "event_type": r.event_type, "app_name": r.app_name,
            "window_title": r.window_title, "url": r.url,
            "duration": r.duration,
            "timestamp_unix": int(r.ts_unix) if r.ts_unix else None,
        }
        for r in events_result.fetchall()
    ]

    return {
        "active_seconds_today": active_seconds,
        "top_apps": top_apps,
        "recent_events": recent_events,
        "device_count": len(device_ids),
    }


# ── Device active-time sync — called by the agent on startup ──────────────────

@router.get("/device-today-active")
async def device_today_active(
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns today's authoritative active-work seconds for this device,
    calculated from stored activity_events (same formula the portal uses).

    The agent calls this on startup so the widget baseline always matches
    the portal — no drift from local daily_totals accumulation errors.
    """
    result = await db.execute(
        text("""
            SELECT COALESCE(SUM(
                CASE
                    WHEN ae.event_type = 'window_change'
                     AND ae.app_name IS NOT NULL
                     AND ae.app_name NOT IN (
                         'System Idle Process','LockApp','LogonUI','Idle','screensaver'
                     )
                    THEN ae.duration ELSE 0
                END
            ), 0) AS active_secs
            FROM activity_events ae
            WHERE ae.device_id = :did
              AND ae.timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
        """),
        {"did": str(device.id)},
    )
    row = result.fetchone()
    return {"active_seconds_today": int(row.active_secs or 0)}
