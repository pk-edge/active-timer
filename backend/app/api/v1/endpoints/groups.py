"""Groups CRUD + nested subgroups + membership + CSV import."""
import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status, Query
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.core import audit
from app.core.group_tree import (
    build_paths, descendant_group_ids, would_create_cycle,
)
from app.db.models import Employee, Device, Group, GroupMember, EmployeeGroupAccess
from app.schemas.rbac import (
    GroupIn, GroupOut, GroupDetailOut, GroupChildOut,
    MemberIn, MemberOut, ImportResult,
)

router = APIRouter()


async def _counts_for_groups(
    group_ids: list[uuid.UUID], db: AsyncSession,
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Member and direct-child counts for many groups in two queries."""
    if not group_ids:
        return {}, {}
    mem_rows = await db.execute(
        select(GroupMember.group_id, func.count())
        .where(GroupMember.group_id.in_(group_ids))
        .group_by(GroupMember.group_id)
    )
    member_counts = {gid: int(c) for gid, c in mem_rows.all()}

    child_rows = await db.execute(
        select(Group.parent_id, func.count())
        .where(
            Group.parent_id.in_(group_ids),
            Group.is_deleted == False,   # noqa: E712
            Group.is_archived == False,  # noqa: E712
        )
        .group_by(Group.parent_id)
    )
    child_counts = {pid: int(c) for pid, c in child_rows.all()}
    return member_counts, child_counts


def _group_out(
    g: Group, member_count: int, child_count: int, path: str | None = None,
) -> GroupOut:
    return GroupOut(
        id=str(g.id), name=g.name, description=g.description,
        parent_id=str(g.parent_id) if g.parent_id else None,
        path=path,
        is_archived=g.is_archived,
        member_count=member_count, child_count=child_count,
        created_at=g.created_at.isoformat() if g.created_at else None,
    )


def _child_out(
    gid: uuid.UUID, name: str, mc: int, cc: int, is_archived: bool = False,
) -> GroupChildOut:
    return GroupChildOut(id=str(gid), name=name, member_count=mc, child_count=cc, is_archived=is_archived)


async def _visible_group_ids(user: Employee, db: AsyncSession) -> set[uuid.UUID] | None:
    """None = all groups in company; otherwise the set the user may see."""
    if getattr(user, "_scope_type", "own") != "groups":
        return None
    rows = await db.execute(
        select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == user.id)
    )
    assigned = [r[0] for r in rows.all()]
    if not assigned:
        return set()
    return await descendant_group_ids(assigned, user.company_id, db)


async def _parse_parent_id(
    raw: str | None, user: Employee, db: AsyncSession,
) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        pid = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid parent_id")
    parent = await db.execute(
        select(Group).where(
            Group.id == pid, Group.company_id == user.company_id, Group.is_deleted == False,  # noqa: E712
        )
    )
    if not parent.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Parent group not found")
    visible = await _visible_group_ids(user, db)
    if visible is not None and pid not in visible:
        raise HTTPException(status_code=403, detail="Cannot create subgroup under this group")
    return pid


async def _check_name_unique(
    company_id: uuid.UUID, name: str, parent_id: uuid.UUID | None,
    db: AsyncSession, exclude_id: uuid.UUID | None = None,
) -> None:
    q = select(Group.id).where(
        Group.company_id == company_id,
        Group.name == name,
        Group.is_deleted == False,  # noqa: E712
        Group.parent_id == parent_id if parent_id else Group.parent_id.is_(None),
    )
    if exclude_id:
        q = q.where(Group.id != exclude_id)
    if (await db.execute(q)).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists at this level")


@router.get("", response_model=list[GroupOut])
async def list_groups(
    include_archived: bool = Query(default=False),
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    filters = [
        Group.company_id == user.company_id,
        Group.is_deleted == False,  # noqa: E712
    ]
    if not include_archived:
        filters.append(Group.is_archived == False)  # noqa: E712
    q = select(Group).where(*filters).order_by(Group.name.asc())
    visible = await _visible_group_ids(user, db)
    if visible is not None:
        if not visible:
            return []
        q = q.where(Group.id.in_(visible))

    groups = list((await db.execute(q)).scalars().all())
    if not groups:
        return []

    gids = [g.id for g in groups]
    member_counts, child_counts = await _counts_for_groups(gids, db)
    paths = build_paths([(g.id, g.name, g.parent_id) for g in groups])

    return [
        _group_out(g, member_counts.get(g.id, 0), child_counts.get(g.id, 0), paths.get(g.id))
        for g in groups
    ]


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupIn,
    user: Employee = Depends(require("groups", "create")),
    db: AsyncSession = Depends(get_db),
):
    parent_id = await _parse_parent_id(body.parent_id, user, db)
    await _check_name_unique(user.company_id, body.name, parent_id, db)

    g = Group(
        company_id=user.company_id, name=body.name, description=body.description,
        parent_id=parent_id, created_by=user.id,
    )
    db.add(g)
    await db.flush()
    await audit.record(db, user, "group.create", "group", str(g.id), {
        "name": g.name, "parent_id": str(parent_id) if parent_id else None,
    })
    await db.commit()
    await db.refresh(g)

    if parent_id:
        # Build full path by loading ancestor chain (avoids KeyError from build_paths
        # when only the new group is in the input list).
        breadcrumb = await _breadcrumb(g, db)
        path = " / ".join([b.name for b in breadcrumb] + [g.name])
    else:
        path = g.name
    return _group_out(g, 0, 0, path)


async def _get_group(group_id: str, user: Employee, db: AsyncSession) -> Group:
    try:
        gid = uuid.UUID(group_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid group id")
    row = await db.execute(
        select(Group).where(
            Group.id == gid, Group.company_id == user.company_id, Group.is_deleted == False,  # noqa: E712
        )
    )
    g = row.scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    visible = await _visible_group_ids(user, db)
    if visible is not None and g.id not in visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return g


async def _members_with_labels(group_id, db: AsyncSession) -> list[MemberOut]:
    rows = await db.execute(
        select(GroupMember).where(GroupMember.group_id == group_id).order_by(GroupMember.added_at.asc())
    )
    members = rows.scalars().all()
    if not members:
        return []

    emp_ids = [m.employee_id for m in members if m.member_type == "employee" and m.employee_id]
    dev_ids = [m.device_id   for m in members if m.member_type == "device"   and m.device_id]

    emp_names: dict = {}
    if emp_ids:
        er = await db.execute(select(Employee.id, Employee.name).where(Employee.id.in_(emp_ids)))
        emp_names = {eid: name for eid, name in er.all()}
    dev_names: dict = {}
    if dev_ids:
        dr = await db.execute(select(Device.id, Device.hostname).where(Device.id.in_(dev_ids)))
        dev_names = {did: host for did, host in dr.all()}

    out: list[MemberOut] = []
    for m in members:
        if m.member_type == "employee" and m.employee_id:
            label = emp_names.get(m.employee_id) or "(unknown user)"
        elif m.member_type == "device" and m.device_id:
            label = dev_names.get(m.device_id) or "(unknown device)"
        else:
            label = ""
        out.append(MemberOut(
            id=str(m.id), member_type=m.member_type,
            employee_id=str(m.employee_id) if m.employee_id else None,
            device_id=str(m.device_id) if m.device_id else None,
            label=label,
        ))
    return out


async def _breadcrumb(g: Group, db: AsyncSession) -> list[GroupChildOut]:
    """Ancestors root → parent (excludes the group itself)."""
    if not g.parent_id:
        return []
    rows = await db.execute(
        text(
            """
            WITH RECURSIVE up AS (
                SELECT id, name, parent_id FROM groups
                WHERE id = :pid AND is_deleted = FALSE
                UNION ALL
                SELECT g.id, g.name, g.parent_id FROM groups g
                JOIN up u ON g.id = u.parent_id
                WHERE g.is_deleted = FALSE
            )
            SELECT id, name FROM up
            """
        ),
        {"pid": str(g.parent_id)},
    )
    chain = [(r[0], r[1]) for r in rows.all()]
    chain.reverse()
    if not chain:
        return []
    gids = [r[0] for r in chain]
    mc, cc = await _counts_for_groups(gids, db)
    return [_child_out(gid, name, mc.get(gid, 0), cc.get(gid, 0)) for gid, name in chain]


@router.get("/{group_id}", response_model=GroupDetailOut)
async def get_group(
    group_id: str,
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    members = await _members_with_labels(g.id, db)

    # Fetch all non-deleted children (active + archived) so the parent detail page
    # can display and manage archived subgroups without a separate request.
    child_rows = await db.execute(
        select(Group).where(Group.parent_id == g.id, Group.is_deleted == False)  # noqa: E712
        .order_by(Group.is_archived.asc(), Group.name.asc())
    )
    children = list(child_rows.scalars().all())
    child_gids = [c.id for c in children]
    mc_map, cc_map = await _counts_for_groups([g.id, *child_gids], db)
    breadcrumb = await _breadcrumb(g, db)
    path = " / ".join([b.name for b in breadcrumb] + [g.name]) if breadcrumb else g.name

    return GroupDetailOut(
        id=str(g.id), name=g.name, description=g.description,
        parent_id=str(g.parent_id) if g.parent_id else None,
        path=path,
        is_archived=g.is_archived,
        member_count=mc_map.get(g.id, len(members)),
        child_count=cc_map.get(g.id, 0),
        created_at=g.created_at.isoformat() if g.created_at else None,
        members=members,
        children=[
            _child_out(c.id, c.name, mc_map.get(c.id, 0), cc_map.get(c.id, 0), c.is_archived)
            for c in children
        ],
        breadcrumb=breadcrumb,
    )


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: str,
    body: GroupIn,
    user: Employee = Depends(require("groups", "edit")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    new_parent = g.parent_id
    if body.parent_id is not None:
        if body.parent_id == "":
            new_parent = None
        else:
            new_parent = await _parse_parent_id(body.parent_id, user, db)
            if await would_create_cycle(g.id, new_parent, user.company_id, db):
                raise HTTPException(status_code=400, detail="Cannot set parent: would create a cycle")

    await _check_name_unique(user.company_id, body.name, new_parent, db, exclude_id=g.id)

    g.name = body.name
    g.description = body.description
    g.parent_id = new_parent
    await audit.record(db, user, "group.update", "group", group_id, {
        "name": body.name, "parent_id": str(new_parent) if new_parent else None,
    })
    await db.commit()
    await db.refresh(g)
    mc, cc = await _counts_for_groups([g.id], db)
    if g.parent_id:
        breadcrumb = await _breadcrumb(g, db)
        path = " / ".join([b.name for b in breadcrumb] + [g.name])
    else:
        path = g.name
    return _group_out(g, mc.get(g.id, 0), cc.get(g.id, 0), path)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: str,
    user: Employee = Depends(require("groups", "delete")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    descendants = await descendant_group_ids([g.id], user.company_id, db)
    to_delete = list(descendants)
    rows = await db.execute(
        select(Group).where(Group.id.in_(to_delete), Group.is_deleted == False)  # noqa: E712
    )
    for grp in rows.scalars().all():
        grp.is_deleted = True
    await audit.record(db, user, "group.delete", "group", group_id, {
        "name": g.name, "cascade_count": len(to_delete) - 1,
    })
    await db.commit()


@router.post("/{group_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_group(
    group_id: str,
    user: Employee = Depends(require("groups", "edit")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    g.is_archived = True
    await audit.record(db, user, "group.archive", "group", group_id, {"name": g.name})
    await db.commit()


@router.post("/{group_id}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_group(
    group_id: str,
    user: Employee = Depends(require("groups", "edit")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    g.is_archived = False
    await audit.record(db, user, "group.unarchive", "group", group_id, {"name": g.name})
    await db.commit()


@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: str,
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    return await _members_with_labels(g.id, db)


@router.post("/{group_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    group_id: str,
    body: MemberIn,
    user: Employee = Depends(require("groups", "edit")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    if body.member_type == "employee":
        if not body.employee_id:
            raise HTTPException(status_code=400, detail="employee_id required")
        emp = await db.execute(select(Employee).where(
            Employee.id == uuid.UUID(body.employee_id), Employee.company_id == user.company_id))
        target = emp.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Employee not found")
        m = GroupMember(group_id=g.id, member_type="employee", employee_id=target.id)
        label = target.name
    elif body.member_type == "device":
        if not body.device_id:
            raise HTTPException(status_code=400, detail="device_id required")
        dev = await db.execute(select(Device).where(
            Device.id == uuid.UUID(body.device_id), Device.company_id == user.company_id))
        target = dev.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Device not found")
        m = GroupMember(group_id=g.id, member_type="device", device_id=target.id)
        label = target.hostname
    else:
        raise HTTPException(status_code=400, detail="member_type must be 'employee' or 'device'")

    db.add(m)
    await audit.record(db, user, "group.member.add", "group", group_id, {
        "group_name": g.name, "member_type": body.member_type, "label": label,
    })
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Member already in group")
    await db.refresh(m)
    return MemberOut(
        id=str(m.id), member_type=m.member_type,
        employee_id=str(m.employee_id) if m.employee_id else None,
        device_id=str(m.device_id) if m.device_id else None,
        label=label,
    )


@router.delete("/{group_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: str,
    member_id: str,
    user: Employee = Depends(require("groups", "edit")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    try:
        mid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid member id")
    row = await db.execute(
        select(GroupMember).where(GroupMember.id == mid, GroupMember.group_id == g.id)
    )
    m = row.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    await audit.record(db, user, "group.member.remove", "group", group_id, {
        "group_name": g.name, "member_type": m.member_type,
        "employee_id": str(m.employee_id) if m.employee_id else None,
        "device_id": str(m.device_id) if m.device_id else None,
    })
    await db.delete(m)
    await db.commit()


@router.post("/import", response_model=ImportResult)
async def import_groups(
    file: UploadFile = File(...),
    user: Employee = Depends(require("groups", "create")),
    db: AsyncSession = Depends(get_db),
):
    """CSV columns: group, identifier, type [, parent]
    type = 'employee'|'device'; identifier = email or hostname.
    parent = optional parent group name (top-level if omitted)."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    created_groups = 0
    added_members = 0
    skipped: list[dict] = []

    rows: list[tuple[int, str, str, str, str | None]] = []
    for i, row in enumerate(reader, start=2):
        gname = (row.get("group") or "").strip()
        ident = (row.get("identifier") or "").strip()
        mtype = (row.get("type") or "").strip().lower()
        parent_name = (row.get("parent") or "").strip() or None
        if not gname or not ident or mtype not in ("employee", "device"):
            skipped.append({"row": i, "reason": "missing/invalid group, identifier, or type"})
            continue
        rows.append((i, gname, ident, mtype, parent_name))

    if not rows:
        return ImportResult(created_groups=0, added_members=0, skipped=skipped)

    gnames = {gname for _, gname, _, _, _ in rows}
    parent_names = {p for _, _, _, _, p in rows if p}
    emails = {ident.lower() for _, _, ident, mtype, _ in rows if mtype == "employee"}
    hosts  = {ident for _, _, ident, mtype, _ in rows if mtype == "device"}

    all_names = gnames | parent_names
    existing = list((await db.execute(
        select(Group).where(Group.company_id == user.company_id, Group.name.in_(all_names), Group.is_deleted == False)  # noqa: E712
    )).scalars().all())
    # Key by (parent_id sentinel, name) — root groups use None parent.
    groups_by_key: dict[tuple, Group] = {}
    for g in existing:
        groups_by_key[(g.parent_id, g.name)] = g

    emps_by_email: dict = {}
    if emails:
        emps_by_email = {
            e.email: e for e in (await db.execute(
                select(Employee).where(
                    Employee.company_id == user.company_id, Employee.email.in_(emails))
            )).scalars().all()
        }
    devs_by_host: dict = {}
    if hosts:
        devs_by_host = {
            d.hostname: d for d in (await db.execute(
                select(Device).where(
                    Device.company_id == user.company_id, Device.hostname.in_(hosts))
            )).scalars().all()
        }

    def _resolve_parent_id(parent_name: str | None) -> uuid.UUID | None:
        if not parent_name:
            return None
        for (pid, name), grp in groups_by_key.items():
            if name == parent_name and pid is None:
                return grp.id
        return None

    needed: list[tuple[str, str | None]] = []
    for _, gname, _, _, parent_name in rows:
        pid = _resolve_parent_id(parent_name)
        if (pid, gname) not in groups_by_key:
            needed.append((gname, parent_name))

    for gname, parent_name in dict.fromkeys(needed):
        pid = _resolve_parent_id(parent_name)
        if parent_name and pid is None:
            pg = Group(company_id=user.company_id, name=parent_name, created_by=user.id)
            db.add(pg)
            await db.flush()
            groups_by_key[(None, parent_name)] = pg
            pid = pg.id
            created_groups += 1
        if (pid, gname) in groups_by_key:
            continue
        g = Group(company_id=user.company_id, name=gname, parent_id=pid, created_by=user.id)
        db.add(g)
        await db.flush()
        groups_by_key[(pid, gname)] = g
        created_groups += 1

    seen: set[tuple] = set()
    gids = [g.id for g in groups_by_key.values()]
    for gid, eid, did in (await db.execute(
        select(GroupMember.group_id, GroupMember.employee_id, GroupMember.device_id)
        .where(GroupMember.group_id.in_(gids))
    )).all():
        seen.add((gid, eid, did))

    for i, gname, ident, mtype, parent_name in rows:
        pid = _resolve_parent_id(parent_name)
        g = groups_by_key.get((pid, gname))
        if not g:
            skipped.append({"row": i, "reason": f"group not found: {gname}"})
            continue
        if mtype == "employee":
            emp = emps_by_email.get(ident.lower())
            if not emp:
                skipped.append({"row": i, "reason": f"employee email not found: {ident}"})
                continue
            key = (g.id, emp.id, None)
            if key in seen:
                skipped.append({"row": i, "reason": "duplicate membership"})
                continue
            db.add(GroupMember(group_id=g.id, member_type="employee", employee_id=emp.id))
        else:
            dev = devs_by_host.get(ident)
            if not dev:
                skipped.append({"row": i, "reason": f"device hostname not found: {ident}"})
                continue
            key = (g.id, None, dev.id)
            if key in seen:
                skipped.append({"row": i, "reason": "duplicate membership"})
                continue
            db.add(GroupMember(group_id=g.id, member_type="device", device_id=dev.id))
        seen.add(key)
        added_members += 1

    await db.commit()
    return ImportResult(created_groups=created_groups, added_members=added_members, skipped=skipped)
