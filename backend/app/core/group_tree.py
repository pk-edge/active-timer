"""Shared helpers for nested groups — descendant expansion and device resolution.

Used by scoping, analytics filters, and the groups API so hierarchy logic lives
in one place.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def descendant_group_ids(
    root_ids: list[uuid.UUID],
    company_id: uuid.UUID,
    db: AsyncSession,
) -> set[uuid.UUID]:
    """Return root_ids plus every non-deleted descendant within the company."""
    if not root_ids:
        return set()
    rows = await db.execute(
        text(
            """
            WITH RECURSIVE down AS (
                SELECT id FROM groups
                WHERE id = ANY(:roots) AND company_id = :cid AND is_deleted = FALSE
                UNION ALL
                SELECT g.id FROM groups g
                JOIN down d ON g.parent_id = d.id
                WHERE g.company_id = :cid AND g.is_deleted = FALSE
            )
            SELECT id FROM down
            """
        ),
        {"roots": [str(i) for i in root_ids], "cid": str(company_id)},
    )
    return {r[0] for r in rows.all()}


async def devices_for_groups(
    group_ids: set[uuid.UUID],
    db: AsyncSession,
) -> list[uuid.UUID]:
    """Distinct device ids from direct members of the given groups."""
    if not group_ids:
        return []
    rows = await db.execute(
        text(
            """
            SELECT DISTINCT COALESCE(gm.device_id, d.id) AS did
            FROM group_members gm
            LEFT JOIN devices d ON d.employee_id = gm.employee_id
                AND gm.member_type = 'employee'
            WHERE gm.group_id = ANY(:gids)
              AND COALESCE(gm.device_id, d.id) IS NOT NULL
            """
        ),
        {"gids": [str(i) for i in group_ids]},
    )
    return [r[0] for r in rows.all()]


def build_paths(
    groups: list[tuple[uuid.UUID, str, uuid.UUID | None]],
) -> dict[uuid.UUID, str]:
    """Build breadcrumb paths (Root / Child / Leaf) from a flat group list."""
    names = {gid: name for gid, name, _ in groups}
    parents = {gid: pid for gid, _, pid in groups}

    def _path(gid: uuid.UUID) -> str:
        parts: list[str] = []
        cur: uuid.UUID | None = gid
        seen: set[uuid.UUID] = set()
        while cur and cur not in seen and cur in names:
            seen.add(cur)
            parts.append(names[cur])
            cur = parents.get(cur)
        parts.reverse()
        return " / ".join(parts)

    return {gid: _path(gid) for gid, _, _ in groups}


async def would_create_cycle(
    group_id: uuid.UUID,
    new_parent_id: uuid.UUID,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """True if setting new_parent_id on group_id would create a hierarchy cycle."""
    if group_id == new_parent_id:
        return True
    descendants = await descendant_group_ids([group_id], company_id, db)
    return new_parent_id in descendants
