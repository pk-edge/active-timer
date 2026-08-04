"""Notification feed for dashboard users.

Emits two kinds of live-computed notifications — nothing is persisted except
read receipts, so both are recomputed fresh on every poll:
  - approval_request : pending user registrations, for anyone with users.edit.
  - talent_offline    : talent devices offline past the company's configured
                        threshold, for Super Admins (company-wide) and Relation
                        Managers (limited to their assigned groups + subgroups).
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_current_user
from app.api.v1.endpoints.company import DEFAULT_SETTINGS
from app.core.group_tree import build_paths, descendant_group_ids, devices_for_groups_detailed
from app.core.provisioning import is_talent_role
from app.core.scoping import ALL, visible_device_ids
from app.db.models import Device, Employee, EmployeeGroupAccess, Group, NotificationRead

router = APIRouter()


def _can_manage_users(user: Employee) -> bool:
    scope = getattr(user, "_scope_type", "own")
    if scope == "all":
        return True
    perms: set = getattr(user, "_perms", set())
    return ("users", "edit") in perms


async def _offline_alert_settings(company_id: uuid.UUID, db: AsyncSession) -> tuple[bool, int]:
    """(enabled, threshold_minutes) — degrades gracefully pre-migration-031."""
    try:
        stored = (await db.execute(
            text("SELECT settings FROM companies WHERE id = :id"),
            {"id": str(company_id)},
        )).scalar() or {}
    except ProgrammingError:
        await db.rollback()
        stored = {}
    merged = {**DEFAULT_SETTINGS, **stored}
    return bool(merged.get("offline_alert_enabled", True)), int(merged.get("offline_alert_mins", 30))


async def _talent_offline_notifications(
    user: Employee, read_keys: set[str], db: AsyncSession,
) -> list[dict]:
    scope = getattr(user, "_scope_type", "own")
    if scope not in ("all", "groups"):
        return []

    enabled, threshold_mins = await _offline_alert_settings(user.company_id, db)
    if not enabled or threshold_mins <= 0:
        return []

    visible = await visible_device_ids(user, db)
    if visible is not ALL and not visible:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_mins)
    q = (
        select(Device, Employee)
        .join(Employee, Employee.id == Device.employee_id)
        .options(selectinload(Employee.role_obj))
        .where(
            Device.company_id == user.company_id,
            Device.is_active == True,      # noqa: E712
            Device.is_archived == False,   # noqa: E712
            Device.is_deleted == False,    # noqa: E712
            Device.last_seen.isnot(None),
            Device.last_seen < cutoff,
        )
    )
    if visible is not ALL:
        q = q.where(Device.id.in_(visible))
    rows = (await db.execute(q)).all()
    candidates = [(d, e) for d, e in rows if is_talent_role(e.role_obj)]
    if not candidates:
        return []

    if scope == "groups":
        ega_rows = await db.execute(
            select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == user.id)
        )
        path_group_ids = await descendant_group_ids([r[0] for r in ega_rows.all()], user.company_id, db)
    else:
        gid_rows = await db.execute(
            select(Group.id).where(Group.company_id == user.company_id, Group.is_deleted == False)  # noqa: E712
        )
        path_group_ids = {r[0] for r in gid_rows.all()}

    device_group_map = await devices_for_groups_detailed(path_group_ids, db)
    path_rows = await db.execute(
        select(Group.id, Group.name, Group.parent_id).where(
            Group.id.in_(path_group_ids), Group.company_id == user.company_id, Group.is_deleted == False,  # noqa: E712
        )
    )
    paths = build_paths([(gid, name, pid) for gid, name, pid in path_rows.all()])

    notifications: list[dict] = []
    for device, emp in candidates:
        minutes = int((datetime.now(timezone.utc) - device.last_seen).total_seconds() // 60)
        key = f"talent_offline:{device.id}:{device.last_seen.isoformat()}"
        group_id = device_group_map.get(device.id)
        path = paths.get(group_id) if group_id else None
        body_parts = [device.hostname, f"offline for {minutes} min"]
        if path:
            body_parts.append(path)
        body_parts.append(f"last seen {device.last_seen.strftime('%b %d, %I:%M %p UTC')}")
        notifications.append({
            "key": key,
            "type": "talent_offline",
            "title": f"⚠ {emp.name}'s device is offline",
            "body": " · ".join(body_parts),
            "created_at": device.last_seen.isoformat(),
            "is_read": key in read_keys,
            "metadata": {"device_id": str(device.id)},
        })
    return notifications


@router.get("")
async def list_notifications(
    user: Employee = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notifications: list[dict] = []

    read_result = await db.execute(
        select(NotificationRead.notification_key).where(
            NotificationRead.employee_id == user.id
        )
    )
    read_keys: set[str] = {r[0] for r in read_result.all()}

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

    notifications.extend(await _talent_offline_notifications(user, read_keys, db))

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
