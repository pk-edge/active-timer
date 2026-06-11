"""Notification feed for dashboard users.

Currently emits approval-request notifications (pending user registrations)
for anyone with users.edit permission. Additional types can be added here.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.db.models import Employee, NotificationRead, RolePermission

router = APIRouter()


def _can_manage_users(user: Employee) -> bool:
    scope = getattr(user, "_scope_type", "own")
    if scope == "all":
        return True
    perms: set = getattr(user, "_perms", set())
    return ("users", "edit") in perms


@router.get("")
async def list_notifications(
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notifications: list[dict] = []

    if _can_manage_users(user):
        pending_result = await db.execute(
            select(Employee)
            .where(
                Employee.company_id == user.company_id,
                Employee.is_approved == False,   # noqa: E712
                Employee.is_active == True,       # noqa: E712
                Employee.is_deleted == False,     # noqa: E712
            )
            .order_by(Employee.created_at.desc())
        )
        pending = pending_result.scalars().all()

        read_result = await db.execute(
            select(NotificationRead.notification_key).where(
                NotificationRead.employee_id == user.id
            )
        )
        read_keys: set[str] = {r[0] for r in read_result.all()}

        for p in pending:
            key = f"approval:{p.id}"
            notifications.append({
                "key": key,
                "type": "approval_request",
                "title": f"{p.name} requested access",
                "body": p.email,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "is_read": key in read_keys,
                "metadata": {"user_id": str(p.id)},
            })

    return notifications


class MarkReadBody(BaseModel):
    keys: list[str]


@router.post("/mark-read", status_code=204)
async def mark_read(
    body: MarkReadBody,
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not body.keys:
        return

    # Single multi-row insert; dedupe so the same key isn't sent twice.
    await db.execute(
        pg_insert(NotificationRead)
        .values([
            {"employee_id": user.id, "notification_key": key}
            for key in dict.fromkeys(body.keys)
        ])
        .on_conflict_do_nothing()
    )
    await db.commit()
