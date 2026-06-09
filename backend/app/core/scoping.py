"""Single source of truth for which devices a dashboard user may see.

Usage in an endpoint:

    ids = await visible_device_ids(user, db)
    if ids is ALL:
        # apply company filter only (admin / scope='all')
    elif not ids:
        return []          # user has access to nothing yet
    else:
        # filter to Device.id in ids  (or device_id = ANY(:ids) in raw SQL)
"""
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Device, GroupMember, EmployeeGroupAccess

# Sentinel meaning "no device-level restriction" (company filter still applies).
ALL = object()


async def visible_device_ids(user, db: AsyncSession):
    """Return ALL, or a list[UUID] of device ids the user is allowed to see."""
    scope = getattr(user, "_scope_type", "own")

    if scope == "all":
        return ALL

    if scope == "own":
        rows = await db.execute(select(Device.id).where(Device.employee_id == user.id))
        return [r[0] for r in rows.all()]

    # scope == "groups"
    gid_rows = await db.execute(
        select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == user.id)
    )
    gids = [g[0] for g in gid_rows.all()]
    if not gids:
        return []

    # Devices added to the group directly.
    direct = await db.execute(
        select(GroupMember.device_id).where(
            GroupMember.group_id.in_(gids),
            GroupMember.member_type == "device",
        )
    )
    # Devices owned by employees who are members of the group.
    via_emp = await db.execute(
        text(
            """
            SELECT d.id FROM devices d
            JOIN group_members gm ON gm.employee_id = d.employee_id
            WHERE gm.group_id = ANY(:gids) AND gm.member_type = 'employee'
            """
        ),
        {"gids": [str(g) for g in gids]},
    )

    ids = {r[0] for r in direct.all() if r[0]} | {r[0] for r in via_emp.all()}
    return list(ids)
