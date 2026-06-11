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

from app.db.models import Device

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

    # scope == "groups" — one round-trip: groups the user may access, their direct
    # device members, and devices owned by employee members, all in a single UNION.
    rows = await db.execute(
        text(
            """
            WITH my_groups AS (
                SELECT group_id FROM employee_group_access WHERE employee_id = :uid
            )
            SELECT gm.device_id AS id
            FROM group_members gm
            JOIN my_groups mg ON mg.group_id = gm.group_id
            WHERE gm.member_type = 'device' AND gm.device_id IS NOT NULL
            UNION
            SELECT d.id
            FROM devices d
            JOIN group_members gm ON gm.employee_id = d.employee_id
            JOIN my_groups mg ON mg.group_id = gm.group_id
            WHERE gm.member_type = 'employee'
            """
        ),
        {"uid": str(user.id)},
    )
    return [r[0] for r in rows.all()]
