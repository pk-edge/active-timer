"""Audit log viewer — filterable, paginated."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.db.models import Employee

router = APIRouter()


@router.get("")
async def list_audit(
    limit:       int           = Query(default=50, ge=1, le=500),
    offset:      int           = Query(default=0, ge=0),
    action:      Optional[str] = Query(default=None, description="Filter by action (partial match)"),
    target_type: Optional[str] = Query(default=None, description="Filter by target type"),
    actor_id:    Optional[str] = Query(default=None, description="Filter by actor employee UUID"),
    since:       Optional[date] = Query(default=None, description="Earliest date (YYYY-MM-DD, inclusive)"),
    until:       Optional[date] = Query(default=None, description="Latest date (YYYY-MM-DD, inclusive)"),
    search:      Optional[str] = Query(default=None, description="Full-text search across action, actor, target"),
    user: Employee = Depends(require("audit", "view")),
    db: AsyncSession = Depends(get_db),
):
    params: dict = {"cid": str(user.company_id), "limit": limit, "offset": offset}

    filters = ["a.company_id = :cid"]

    if action:
        filters.append("a.action ILIKE :action")
        params["action"] = f"%{action}%"

    if target_type:
        filters.append("a.target_type = :target_type")
        params["target_type"] = target_type

    if actor_id:
        filters.append("a.actor_employee_id = CAST(:actor_id AS uuid)")
        params["actor_id"] = actor_id

    if since:
        filters.append("a.created_at >= CAST(:since AS date)")
        params["since"] = since

    if until:
        filters.append("a.created_at < (CAST(:until AS date) + INTERVAL '1 day')")
        params["until"] = until

    if search:
        filters.append("""(
            a.action ILIKE :search
            OR e.name ILIKE :search
            OR e.email ILIKE :search
            OR a.target_id ILIKE :search
            OR a.actor_role ILIKE :search
            OR a.metadata::text ILIKE :search
        )""")
        params["search"] = f"%{search}%"

    where = " AND ".join(filters)

    # Count query for pagination metadata
    count_result = await db.execute(
        text(f"""
            SELECT COUNT(*) FROM audit_log a
            LEFT JOIN employees e ON e.id = a.actor_employee_id
            WHERE {where}
        """),
        params,
    )
    total = int(count_result.scalar() or 0)

    rows = await db.execute(
        text(f"""
            SELECT a.id, a.action, a.target_type, a.target_id, a.metadata,
                   a.actor_role, a.created_at,
                   e.id AS actor_id, e.name AS actor_name, e.email AS actor_email
            FROM audit_log a
            LEFT JOIN employees e ON e.id = a.actor_employee_id
            WHERE {where}
            ORDER BY a.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )

    entries = [
        {
            "id": str(r.id),
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "metadata": r.metadata,
            "actor_id": str(r.actor_id) if r.actor_id else None,
            "actor_name": r.actor_name,
            "actor_email": r.actor_email,
            "actor_role": r.actor_role,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_at_unix": int(r.created_at.timestamp()) if r.created_at else None,
        }
        for r in rows.fetchall()
    ]

    return {"total": total, "offset": offset, "limit": limit, "entries": entries}


@router.get("/filters")
async def audit_filter_options(
    user: Employee = Depends(require("audit", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Distinct action verbs and target types for populating filter dropdowns."""
    actions_res = await db.execute(
        text("SELECT DISTINCT action FROM audit_log WHERE company_id = :cid ORDER BY action"),
        {"cid": str(user.company_id)},
    )
    targets_res = await db.execute(
        text("SELECT DISTINCT target_type FROM audit_log WHERE company_id = :cid AND target_type IS NOT NULL ORDER BY target_type"),
        {"cid": str(user.company_id)},
    )
    actors_res = await db.execute(
        text("""
            SELECT DISTINCT e.id, e.name, e.email
            FROM audit_log a
            JOIN employees e ON e.id = a.actor_employee_id
            WHERE a.company_id = :cid
            ORDER BY e.name
        """),
        {"cid": str(user.company_id)},
    )
    return {
        "actions": [r.action for r in actions_res.fetchall()],
        "target_types": [r.target_type for r in targets_res.fetchall()],
        "actors": [{"id": str(r.id), "name": r.name, "email": r.email} for r in actors_res.fetchall()],
    }
