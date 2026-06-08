"""Users CRUD + role/group/password assignment.

A "user" is an Employee row. Monitored people may have no password/role; dashboard
users (admins, RMs) have a password and an assigned role. Group assignment here
controls which groups a 'groups'-scoped user may VIEW (employee_group_access).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, delete, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.core import audit
from app.core.security import hash_password
from app.db.models import Employee, Device, Role, EmployeeGroupAccess, Group, GroupMember
from app.schemas.rbac import UserIn, UserPatch, UserOut, ApproveRequest

router = APIRouter()


async def _device_count(emp_id, db: AsyncSession) -> int:
    r = await db.execute(select(func.count()).select_from(Device).where(Device.employee_id == emp_id))
    return int(r.scalar() or 0)


async def _group_ids(emp_id, db: AsyncSession) -> list[str]:
    r = await db.execute(select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == emp_id))
    return [str(g[0]) for g in r.all()]


async def _user_out(emp: Employee, db: AsyncSession) -> UserOut:
    return UserOut(
        id=str(emp.id), name=emp.name, email=emp.email,
        role_id=str(emp.role_id) if emp.role_id else None,
        role_name=emp.role_obj.name if emp.role_obj else (emp.role or None),
        scope_type=emp.role_obj.scope_type if emp.role_obj else None,
        is_active=emp.is_active,
        is_approved=getattr(emp, "is_approved", True),
        group_ids=await _group_ids(emp.id, db),
        device_count=await _device_count(emp.id, db),
    )


async def _resolve_role(role_id: str | None, company_id, db: AsyncSession) -> Role | None:
    if not role_id:
        return None
    try:
        rid = uuid.UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role id")
    r = await db.execute(select(Role).where(Role.id == rid, Role.company_id == company_id))
    role = r.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


async def _set_group_access(emp_id, company_id, group_ids: list[str], db: AsyncSession) -> None:
    await db.execute(delete(EmployeeGroupAccess).where(EmployeeGroupAccess.employee_id == emp_id))
    for gid in group_ids:
        try:
            guid = uuid.UUID(gid)
        except ValueError:
            continue
        exists = await db.execute(select(Group.id).where(Group.id == guid, Group.company_id == company_id))
        if exists.first():
            db.add(EmployeeGroupAccess(employee_id=emp_id, group_id=guid))


@router.get("", response_model=list[UserOut])
async def list_users(
    search:  str | None = Query(default=None),
    pending: bool = Query(default=False),
    user: Employee = Depends(require("users", "view")),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Employee).options(selectinload(Employee.role_obj))
        .where(Employee.company_id == user.company_id, Employee.is_deleted == False)  # noqa: E712
        .order_by(Employee.created_at.asc())
    )
    if pending:
        q = q.where(Employee.is_approved == False, Employee.is_active == True)  # noqa: E712
    else:
        q = q.where(Employee.is_approved == True)  # noqa: E712
    if search:
        like = f"%{search.lower()}%"
        q = q.where(func.lower(Employee.name).like(like) | func.lower(Employee.email).like(like))

    # Groups-scoped users only see employees who belong to their assigned groups.
    if getattr(user, "_scope_type", "own") == "groups":
        gid_rows = await db.execute(
            select(EmployeeGroupAccess.group_id)
            .where(EmployeeGroupAccess.employee_id == user.id)
        )
        my_gids = [g[0] for g in gid_rows.all()]
        if not my_gids:
            return []
        # Employees who are direct group members
        q = q.where(
            Employee.id.in_(
                select(GroupMember.employee_id)
                .where(
                    GroupMember.group_id.in_(my_gids),
                    GroupMember.member_type == "employee",
                    GroupMember.employee_id.isnot(None),
                )
            )
        )

    rows = await db.execute(q)
    employees = rows.scalars().all()
    if not employees:
        return []

    emp_ids = [e.id for e in employees]

    # Batch device counts + group access in 2 queries total (was 2 per user → N+1).
    dc_rows = await db.execute(
        select(Device.employee_id, func.count())
        .where(Device.employee_id.in_(emp_ids))
        .group_by(Device.employee_id)
    )
    device_counts = {eid: int(c) for eid, c in dc_rows.all()}

    ga_rows = await db.execute(
        select(EmployeeGroupAccess.employee_id, EmployeeGroupAccess.group_id)
        .where(EmployeeGroupAccess.employee_id.in_(emp_ids))
    )
    groups_by_emp: dict = {}
    for eid, gid in ga_rows.all():
        groups_by_emp.setdefault(eid, []).append(str(gid))

    return [
        UserOut(
            id=str(e.id), name=e.name, email=e.email,
            role_id=str(e.role_id) if e.role_id else None,
            role_name=e.role_obj.name if e.role_obj else (e.role or None),
            scope_type=e.role_obj.scope_type if e.role_obj else None,
            is_active=e.is_active,
            is_approved=getattr(e, "is_approved", True),
            group_ids=groups_by_emp.get(e.id, []),
            device_count=device_counts.get(e.id, 0),
        )
        for e in employees
    ]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserIn,
    user: Employee = Depends(require("users", "create")),
    db: AsyncSession = Depends(get_db),
):
    dup = await db.execute(select(Employee).where(Employee.email == body.email.lower()))
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")

    role = await _resolve_role(body.role_id, user.company_id, db)
    emp = Employee(
        company_id=user.company_id,
        email=body.email.lower(),
        name=body.name,
        role="employee",
        role_id=role.id if role else None,
        password_hash=hash_password(body.password) if body.password else None,
        is_active=True,
    )
    db.add(emp)
    await db.flush()
    await _set_group_access(emp.id, user.company_id, body.group_ids, db)
    await audit.record(db, user, "user.create", "user", str(emp.id), {"email": emp.email})
    await db.commit()

    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


async def _get_user(user_id: str, company_id, db: AsyncSession) -> Employee:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user id")
    r = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj))
        .where(Employee.id == uid, Employee.company_id == company_id)
    )
    emp = r.scalar_one_or_none()
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return emp


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: str,
    user: Employee = Depends(require("users", "view")),
    db: AsyncSession = Depends(get_db),
):
    return await _user_out(await _get_user(user_id, user.company_id, db), db)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserPatch,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    if body.name is not None:
        emp.name = body.name
    if body.role_id is not None:
        role = await _resolve_role(body.role_id, user.company_id, db)
        emp.role_id = role.id if role else None
    if body.password:
        emp.password_hash = hash_password(body.password)
    if body.is_active is not None:
        emp.is_active = body.is_active
    if body.group_ids is not None:
        await _set_group_access(emp.id, user.company_id, body.group_ids, db)
    await db.commit()

    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


@router.put("/{user_id}/groups", response_model=UserOut)
async def set_user_groups(
    user_id: str,
    group_ids: list[str],
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    await _set_group_access(emp.id, user.company_id, group_ids, db)
    await db.commit()
    return await _user_out(emp, db)


@router.post("/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str,
    body: ApproveRequest,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    if emp.is_approved:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already approved")

    role = await _resolve_role(body.role_id, user.company_id, db)
    if not role:
        raise HTTPException(status_code=400, detail="role_id is required to approve a user")

    emp.role_id = role.id
    emp.is_approved = True
    await _set_group_access(emp.id, user.company_id, body.group_ids, db)
    await audit.record(db, user, "user.approve", "user", user_id,
                       {"email": emp.email, "role": role.name})
    await db.commit()

    # Expire the object so lazy="joined" re-fetches role_obj on next access.
    await db.refresh(emp)
    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_user(
    user_id: str,
    user: Employee = Depends(require("users", "delete")),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete: flags is_deleted=True. User disappears from all listings."""
    emp = await _get_user(user_id, user.company_id, db)
    if emp.id == user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You cannot delete yourself")
    emp.is_deleted = True
    emp.is_active = False
    await audit.record(db, user, "user.delete", "user", user_id, {"email": emp.email})
    await db.commit()


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: str,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Temporarily deactivate — admin can re-activate later."""
    emp = await _get_user(user_id, user.company_id, db)
    if emp.id == user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You cannot deactivate yourself")
    emp.is_active = False
    await audit.record(db, user, "user.deactivate", "user", user_id, {"email": emp.email})
    await db.commit()
    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


@router.post("/{user_id}/activate", response_model=UserOut)
async def activate_user(
    user_id: str,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Re-activate a previously deactivated user."""
    emp = await _get_user(user_id, user.company_id, db)
    emp.is_active = True
    await audit.record(db, user, "user.activate", "user", user_id, {"email": emp.email})
    await db.commit()
    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)
