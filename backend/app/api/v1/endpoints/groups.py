"""Groups CRUD + membership (employees and/or devices) + CSV import."""
import csv
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.core import audit
from app.db.models import Employee, Device, Group, GroupMember, EmployeeGroupAccess
from app.schemas.rbac import (
    GroupIn, GroupOut, GroupDetailOut, MemberIn, MemberOut, ImportResult,
)

router = APIRouter()


async def _member_count(group_id, db: AsyncSession) -> int:
    r = await db.execute(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
    )
    return int(r.scalar() or 0)


def _group_out(g: Group, count: int) -> GroupOut:
    return GroupOut(
        id=str(g.id), name=g.name, description=g.description,
        member_count=count, created_at=g.created_at.isoformat() if g.created_at else None,
    )


@router.get("", response_model=list[GroupOut])
async def list_groups(
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Group, func.count(GroupMember.id))
        .outerjoin(GroupMember, GroupMember.group_id == Group.id)
        .where(Group.company_id == user.company_id, Group.is_deleted == False)  # noqa: E712
        .group_by(Group.id)
        .order_by(Group.name.asc())
    )

    # Groups-scoped users only see the groups they are explicitly assigned to.
    if getattr(user, "_scope_type", "own") == "groups":
        my_group_ids = select(EmployeeGroupAccess.group_id).where(
            EmployeeGroupAccess.employee_id == user.id
        )
        q = q.where(Group.id.in_(my_group_ids))

    rows = await db.execute(q)
    return [_group_out(g, int(c or 0)) for g, c in rows.all()]


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupIn,
    user: Employee = Depends(require("groups", "create")),
    db: AsyncSession = Depends(get_db),
):
    dup = await db.execute(
        select(Group).where(Group.company_id == user.company_id, Group.name == body.name)
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists")
    g = Group(company_id=user.company_id, name=body.name, description=body.description,
              created_by=user.id)
    db.add(g)
    await db.flush()
    await audit.record(db, user, "group.create", "group", str(g.id), {"name": g.name})
    await db.commit()
    await db.refresh(g)
    return _group_out(g, 0)


async def _get_group(group_id: str, user: Employee, db: AsyncSession) -> Group:
    try:
        gid = uuid.UUID(group_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid group id")
    row = await db.execute(
        select(Group).where(Group.id == gid, Group.company_id == user.company_id)
    )
    g = row.scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return g


async def _members_with_labels(group_id, db: AsyncSession) -> list[MemberOut]:
    rows = await db.execute(
        select(GroupMember).where(GroupMember.group_id == group_id).order_by(GroupMember.added_at.asc())
    )
    members = rows.scalars().all()
    if not members:
        return []

    # Resolve all employee names + device hostnames in 2 queries total
    # (was one query per member → N+1 on large groups).
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


@router.get("/{group_id}", response_model=GroupDetailOut)
async def get_group(
    group_id: str,
    user: Employee = Depends(require("groups", "view")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    members = await _members_with_labels(g.id, db)
    return GroupDetailOut(
        id=str(g.id), name=g.name, description=g.description,
        member_count=len(members), created_at=g.created_at.isoformat() if g.created_at else None,
        members=members,
    )


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: str,
    body: GroupIn,
    user: Employee = Depends(require("groups", "edit")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    g.name = body.name
    g.description = body.description
    await db.commit()
    return _group_out(g, await _member_count(g.id, db))


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: str,
    user: Employee = Depends(require("groups", "delete")),
    db: AsyncSession = Depends(get_db),
):
    g = await _get_group(group_id, user, db)
    # Soft-delete: preserve member history + audit trail
    g.is_deleted = True
    await audit.record(db, user, "group.delete", "group", group_id, {"name": g.name})
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
    await db.delete(m)
    await db.commit()


@router.post("/import", response_model=ImportResult)
async def import_groups(
    file: UploadFile = File(...),
    user: Employee = Depends(require("groups", "create")),
    db: AsyncSession = Depends(get_db),
):
    """CSV columns: group, identifier, type  (type = 'employee'|'device';
    identifier = employee email or device hostname). Header row required."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    created_groups = 0
    added_members = 0
    skipped: list[dict] = []
    group_cache: dict[str, Group] = {}

    for i, row in enumerate(reader, start=2):  # row 1 = header
        gname = (row.get("group") or "").strip()
        ident = (row.get("identifier") or "").strip()
        mtype = (row.get("type") or "").strip().lower()
        if not gname or not ident or mtype not in ("employee", "device"):
            skipped.append({"row": i, "reason": "missing/invalid group, identifier, or type"})
            continue

        g = group_cache.get(gname)
        if g is None:
            existing = await db.execute(
                select(Group).where(Group.company_id == user.company_id, Group.name == gname)
            )
            g = existing.scalar_one_or_none()
            if g is None:
                g = Group(company_id=user.company_id, name=gname, created_by=user.id)
                db.add(g)
                await db.flush()
                created_groups += 1
            group_cache[gname] = g

        if mtype == "employee":
            er = await db.execute(select(Employee).where(
                Employee.company_id == user.company_id, Employee.email == ident.lower()))
            emp = er.scalar_one_or_none()
            if not emp:
                skipped.append({"row": i, "reason": f"employee email not found: {ident}"})
                continue
            dupe = await db.execute(select(GroupMember.id).where(
                GroupMember.group_id == g.id, GroupMember.employee_id == emp.id))
            if dupe.first():
                skipped.append({"row": i, "reason": "duplicate membership"})
                continue
            db.add(GroupMember(group_id=g.id, member_type="employee", employee_id=emp.id))
        else:
            dr = await db.execute(select(Device).where(
                Device.company_id == user.company_id, Device.hostname == ident))
            dev = dr.scalar_one_or_none()
            if not dev:
                skipped.append({"row": i, "reason": f"device hostname not found: {ident}"})
                continue
            dupe = await db.execute(select(GroupMember.id).where(
                GroupMember.group_id == g.id, GroupMember.device_id == dev.id))
            if dupe.first():
                skipped.append({"row": i, "reason": "duplicate membership"})
                continue
            db.add(GroupMember(group_id=g.id, member_type="device", device_id=dev.id))

        await db.flush()
        added_members += 1

    await db.commit()
    return ImportResult(created_groups=created_groups, added_members=added_members, skipped=skipped)
