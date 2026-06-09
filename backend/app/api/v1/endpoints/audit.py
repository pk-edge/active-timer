"""Read-only audit log viewer."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.db.models import Employee

router = APIRouter()


@router.get("")
async def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    user: Employee = Depends(require("audit", "view")),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        text("""
            SELECT a.id, a.action, a.target_type, a.target_id, a.metadata,
                   a.created_at, e.name AS actor_name, e.email AS actor_email
            FROM audit_log a
            LEFT JOIN employees e ON e.id = a.actor_employee_id
            WHERE a.company_id = :cid
            ORDER BY a.created_at DESC
            LIMIT :limit
        """),
        {"cid": str(user.company_id), "limit": limit},
    )
    return [
        {
            "id": str(r.id),
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "metadata": r.metadata,
            "actor_name": r.actor_name,
            "actor_email": r.actor_email,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_at_unix": int(r.created_at.timestamp()) if r.created_at else None,
        }
        for r in rows.fetchall()
    ]
