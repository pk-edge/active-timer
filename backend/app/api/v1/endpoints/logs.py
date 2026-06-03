"""
GET /v1/logs — company-wide activity feed for the dashboard Logs page.
Returns the most recent events across all devices belonging to the admin's company.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import get_db, get_current_admin
from app.db.models import Employee

router = APIRouter()


@router.get("", response_model=list[dict])
async def get_logs(
    since:      Optional[int] = Query(default=None),
    until:      Optional[int] = Query(default=None),
    device_id:  Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    limit:      int = Query(default=200, le=1000),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    params: dict = {"company_id": str(admin.company_id), "limit": limit}
    where_clauses = ["d.company_id = :company_id", "d.is_active = TRUE"]

    if since is not None:
        where_clauses.append("ae.timestamp >= to_timestamp(:since)")
        params["since"] = since
    else:
        default_since = datetime.now(timezone.utc) - timedelta(hours=48)
        where_clauses.append("ae.timestamp >= :default_since")
        params["default_since"] = default_since

    if until is not None:
        where_clauses.append("ae.timestamp < to_timestamp(:until)")
        params["until"] = until

    if device_id:
        where_clauses.append("d.id = :device_id::uuid")
        params["device_id"] = device_id

    if event_type:
        where_clauses.append("ae.event_type = :event_type")
        params["event_type"] = event_type

    where_sql = " AND ".join(where_clauses)

    result = await db.execute(
        text(f"""
            SELECT
                ae.id, ae.event_type, ae.app_name, ae.window_title,
                ae.url, ae.duration, ae.timestamp,
                COALESCE(EXTRACT(EPOCH FROM ae.timestamp)::bigint, 0) AS timestamp_unix,
                d.id       AS device_id,
                d.hostname AS hostname
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE {where_sql}
            ORDER BY ae.timestamp DESC
            LIMIT :limit
        """),
        params,
    )

    rows = result.fetchall()
    return [
        {
            "id":             str(r.id),
            "event_type":     r.event_type,
            "app_name":       r.app_name,
            "window_title":   r.window_title,
            "url":            r.url,
            "duration":       r.duration,
            "timestamp":      r.timestamp.isoformat() if r.timestamp else None,
            "timestamp_unix": r.timestamp_unix,
            "device_id":      str(r.device_id),
            "hostname":       r.hostname,
        }
        for r in rows
    ]
