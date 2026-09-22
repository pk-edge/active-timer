"""Roles CRUD + permission-catalog endpoint."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user, require
from app.core import permissions as perms_catalog
from app.core import audit
from app.core.AdminAuthCache import admin_auth_cache
from app.db.models import Employee, Role, RolePermission
from app.schemas.rbac import RoleIn, RoleOut, PermissionItem

router = APIRouter()


def _role_out(role: Role) -> RoleOut:
    return RoleOut(
        id=str(role.id), name=role.name, description=role.description,
        scope_type=role.scope_type, is_system=role.is_system,
        permissions=[PermissionItem(module=p.module, action=p.action) for p in role.permissions],
    )


@router.get("/modules")
async def list_modules(user: Employee = Depends(require("roles", "view"))):
    """The permission matrix catalog (modules x actions). Drives the UI."""
    return perms_catalog.MODULES


@router.get("", response_model=list[RoleOut])
async def list_roles(
    user: Employee = Depends(require("roles", "view")),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(Role).options(selectinload(Role.permissions))
        .where(Role.company_id == user.company_id, Role.is_deleted == False)  # noqa: E712
        .order_by(Role.created_at.asc())
    )
    return [_role_out(r) for r in rows.scalars().all()]


def _validate_perms(items: list[PermissionItem]) -> None:
    for p in items:
        if not perms_catalog.is_valid(p.module, p.action):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid permission: {p.module}.{p.action}",
            )


def _assert_can_grant(user: Employee, body: RoleIn) -> None:
    """Anti-escalation: you cannot hand out access you do not hold yourself.

    Super Admins hold every permission and all-scope, so they are unaffected.
    This closes the path where a holder of roles.create/roles.edit grants
    themselves (via a role) permissions or data scope beyond their own.
    """
    caller_scope = getattr(user, "_scope_type", "own")
    if body.scope_type == "all" and caller_scope != "all":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an all-scope user can assign a role all-data scope",
        )
    if body.scope_type == "groups" and caller_scope not in ("all", "groups"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an all- or group-scoped user can assign a role group scope",
        )
    # Deliberately NOT filtered through perms_catalog.is_valid() the way
    # users.py::_assert_can_assign_role is. That guard reads the target role's
    # permissions from the DATABASE, where a pair retired from permissions.py
    # can linger and read as one the caller "does not hold". Here the
    # permissions come from the REQUEST, and both call sites run
    # _validate_perms() first, which already rejects anything outside the
    # catalog with a 400 — so a retired pair can never reach this line.
    held = getattr(user, "_perms", set())
    missing = [f"{p.module}.{p.action}" for p in body.permissions
               if (p.module, p.action) not in held]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot grant permissions you do not hold: {', '.join(missing)}",
        )


def _assert_can_touch_system_role(user: Employee, role: Role) -> None:
    """System roles (Super Admin / Viewer / Talent) may only be modified or
    deleted by an all-scope user — a scoped role-manager must not be able to
    rewrite or remove the roles that anchor the permission model."""
    if role.is_system and getattr(user, "_scope_type", "own") != "all":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System roles can only be changed by an all-scope administrator",
        )


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleIn,
    user: Employee = Depends(require("roles", "create")),
    db: AsyncSession = Depends(get_db),
):
    if body.scope_type not in ("all", "groups", "own"):
        raise HTTPException(status_code=400, detail="Invalid scope_type")
    _validate_perms(body.permissions)
    _assert_can_grant(user, body)

    dup = await db.execute(
        select(Role).where(Role.company_id == user.company_id, Role.name == body.name)
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role name already exists")

    role = Role(company_id=user.company_id, name=body.name, description=body.description,
                scope_type=body.scope_type, is_system=False)
    db.add(role)
    await db.flush()
    for p in body.permissions:
        db.add(RolePermission(role_id=role.id, module=p.module, action=p.action))
    await audit.record(db, user, "role.create", "role", str(role.id), {"name": role.name})
    await db.commit()
    admin_auth_cache.invalidate_all()   # role permissions changed — drop cached auth
    await db.refresh(role, attribute_names=["permissions"])
    return _role_out(role)


async def _get_role(role_id: str, user: Employee, db: AsyncSession) -> Role:
    try:
        rid = uuid.UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role id")
    row = await db.execute(
        select(Role).options(selectinload(Role.permissions))
        .where(Role.id == rid, Role.company_id == user.company_id)
    )
    role = row.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


@router.get("/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: str,
    user: Employee = Depends(require("roles", "view")),
    db: AsyncSession = Depends(get_db),
):
    return _role_out(await _get_role(role_id, user, db))


@router.patch("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    body: RoleIn,
    user: Employee = Depends(require("roles", "edit")),
    db: AsyncSession = Depends(get_db),
):
    role = await _get_role(role_id, user, db)
    # System roles are editable, but only by an all-scope administrator.
    _assert_can_touch_system_role(user, role)
    if body.scope_type not in ("all", "groups", "own"):
        raise HTTPException(status_code=400, detail="Invalid scope_type")
    _validate_perms(body.permissions)
    _assert_can_grant(user, body)

    role.name = body.name
    role.description = body.description
    role.scope_type = body.scope_type
    # Replace permission set in one statement instead of one DELETE per row.
    # The ORM-enabled delete also evicts the loaded objects from the session,
    # and the trailing refresh reloads role.permissions for the response.
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    await db.flush()
    for p in body.permissions:
        db.add(RolePermission(role_id=role.id, module=p.module, action=p.action))
    await audit.record(db, user, "role.update", "role", role_id, {
        "name": body.name, "scope_type": body.scope_type,
        "permissions": [f"{p.module}.{p.action}" for p in body.permissions],
    })
    await db.commit()
    admin_auth_cache.invalidate_all()   # role permissions changed — drop cached auth
    await db.refresh(role, attribute_names=["permissions"])
    return _role_out(role)


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    user: Employee = Depends(require("roles", "delete")),
    db: AsyncSession = Depends(get_db),
):
    role = await _get_role(role_id, user, db)
    # Deletable by an all-scope administrator only when it's a system role.
    _assert_can_touch_system_role(user, role)
    # Rather than block when the role is still assigned, clear it from those employees
    # (role_id → NULL) in the same transaction so no one is left pointing at a
    # deleted role. Cleared users fall back to their legacy `role` perms (admins
    # keep full access) or to no permissions until an admin assigns a new role.
    unassigned = (await db.execute(
        update(Employee).where(Employee.role_id == role.id).values(role_id=None)
    )).rowcount
    # Soft-delete: flag instead of physical removal so audit history is preserved
    role.is_deleted = True
    await audit.record(db, user, "role.delete", "role", role_id,
                       {"name": role.name, "unassigned_users": unassigned})
    await db.commit()
    admin_auth_cache.invalidate_all()   # role permissions changed — drop cached auth
