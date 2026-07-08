"""
GET /v1/logs — company-wide activity feed for the dashboard Logs page.
Returns the most recent events across all devices belonging to the admin's company.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import get_db, get_current_admin, require
from app.core.config import get_settings
from app.core.scoping import visible_device_ids, ALL
from app.db.models import Employee

router = APIRouter()
settings = get_settings()


def _require_uuid(value: str, param: str) -> str:
    """400 on malformed ids instead of letting CAST(:x AS uuid) 500 in the DB."""
    try:
        return str(_uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {param}")


@router.get("", response_model=list[dict])
async def get_logs(
    since:       Optional[int] = Query(default=None),
    until:       Optional[int] = Query(default=None),
    device_id:   Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    event_type:  Optional[str] = Query(default=None),
    limit:       int = Query(default=500, le=2000),
    per_device:  Optional[int] = Query(default=None, ge=1, le=500),
    admin: Employee = Depends(require("logs", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Company-wide activity feed with PER-DEVICE FAIRNESS.

    A flat global `ORDER BY timestamp DESC LIMIT n` lets the busiest devices
    monopolize the newest rows, so quieter-but-online devices disappear from the
    page once the fleet grows past ~100 (the reported "logs stop, device still
    online" bug). Instead we take the most-recent `per_device` events for EACH
    visible device via a LATERAL join (index-backed by idx_events_device_ts),
    then merge, order by recency, and cap at `limit`.

    Fairness guarantee: every visible device is represented as long as
    (visible_devices × per_device) ≤ limit. Beyond that the merged feed still
    shows the most-recent events across the WHOLE fleet — but no single busy
    device can crowd out others by more than `per_device` rows, which is the
    behaviour the flat global LIMIT lacked. For a large fleet, narrow the view
    with the device_id/employee_id filters (the per-device timeline shows full
    history) rather than scrolling one global firehose.

    When `device_id` is given the feed is already a single device, so fairness is
    moot and we use the simpler flat query.
    """
    params: dict = {"company_id": str(admin.company_id), "limit": limit}
    ev_clauses: list[str] = []   # filters on the per-device event subquery (ae.*)
    dev_clauses = ["d.company_id = :company_id", "d.is_active = TRUE"]

    # Data scope: restrict to devices the user may see (bound param, not interpolated).
    vids = await visible_device_ids(admin, db)
    if vids is not ALL:
        if not vids:
            return []
        dev_clauses.append("d.id = ANY(:vids)")
        params["vids"] = [str(i) for i in vids]

    if since is not None:
        ev_clauses.append("ae.timestamp >= to_timestamp(:since)")
        params["since"] = since
        # Skip-dead-devices prefilter: a device whose last_seen predates the
        # window start cannot have events in it, so its LATERAL probe would be
        # a guaranteed-empty index descent. On a 2000-device fleet this drops
        # most probes for narrow windows.
        dev_clauses.append("(d.last_seen IS NULL OR d.last_seen >= to_timestamp(:since))")
    else:
        default_since = datetime.now(timezone.utc) - timedelta(hours=48)
        ev_clauses.append("ae.timestamp >= :default_since")
        params["default_since"] = default_since
        dev_clauses.append("(d.last_seen IS NULL OR d.last_seen >= :default_since)")

    if until is not None:
        ev_clauses.append("ae.timestamp < to_timestamp(:until)")
        params["until"] = until

    if employee_id:
        dev_clauses.append("d.employee_id = CAST(:employee_id AS uuid)")
        params["employee_id"] = _require_uuid(employee_id, "employee_id")

    if event_type:
        ev_clauses.append("ae.event_type = :event_type")
        params["event_type"] = event_type

    if device_id:
        # Single device → fairness is irrelevant; flat recency feed.
        dev_clauses.append("d.id = CAST(:device_id AS uuid)")
        params["device_id"] = _require_uuid(device_id, "device_id")
        ev_sql = (" AND " + " AND ".join(ev_clauses)) if ev_clauses else ""
        query = text(f"""
            SELECT
                ae.id, ae.event_type, ae.app_name, ae.window_title,
                ae.url, ae.duration, ae.timestamp,
                COALESCE(EXTRACT(EPOCH FROM ae.timestamp)::bigint, 0) AS timestamp_unix,
                d.id       AS device_id,
                d.hostname AS hostname,
                e.id       AS employee_id,
                e.name     AS employee_name
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            LEFT JOIN employees e ON e.id = d.employee_id
            WHERE {" AND ".join(dev_clauses)}{ev_sql}
            ORDER BY ae.timestamp DESC
            LIMIT :limit
        """)
    else:
        # Whole fleet → top-N events PER device via LATERAL, then merge + cap.
        params["per_device"] = per_device or settings.LOGS_PER_DEVICE_DEFAULT
        ev_sql = (" AND " + " AND ".join(ev_clauses)) if ev_clauses else ""
        query = text(f"""
            SELECT
                ev.id, ev.event_type, ev.app_name, ev.window_title,
                ev.url, ev.duration, ev.timestamp,
                COALESCE(EXTRACT(EPOCH FROM ev.timestamp)::bigint, 0) AS timestamp_unix,
                d.id       AS device_id,
                d.hostname AS hostname,
                e.id       AS employee_id,
                e.name     AS employee_name
            FROM devices d
            LEFT JOIN employees e ON e.id = d.employee_id
            JOIN LATERAL (
                SELECT ae.id, ae.event_type, ae.app_name, ae.window_title,
                       ae.url, ae.duration, ae.timestamp
                FROM activity_events ae
                WHERE ae.device_id = d.id{ev_sql}
                ORDER BY ae.timestamp DESC
                LIMIT :per_device
            ) ev ON TRUE
            WHERE {" AND ".join(dev_clauses)}
            ORDER BY ev.timestamp DESC
            LIMIT :limit
        """)

    result = await db.execute(query, params)

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
            "employee_id":    str(r.employee_id) if r.employee_id else None,
            "employee_name":  r.employee_name,
        }
        for r in rows
    ]
