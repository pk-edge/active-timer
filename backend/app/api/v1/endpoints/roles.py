"""Roles CRUD + permission-catalog endpoint."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user, require
from app.core import permissions as perms_catalog
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
        .where(Role.company_id == user.company_id).order_by(Role.created_at.asc())
    )
    return [_role_out(r) for r in rows.scalars().all()]


def _validate_perms(items: list[PermissionItem]) -> None:
    for p in items:
        if not perms_catalog.is_valid(p.module, p.action):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid permission: {p.module}.{p.action}",
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
    await db.commit()
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
    if role.is_system:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System role cannot be edited")
    if body.scope_type not in ("all", "groups", "own"):
        raise HTTPException(status_code=400, detail="Invalid scope_type")
    _validate_perms(body.permissions)

    role.name = body.name
    role.description = body.description
    role.scope_type = body.scope_type
    # Replace permission set
    for p in list(role.permissions):
        await db.delete(p)
    await db.flush()
    for p in body.permissions:
        db.add(RolePermission(role_id=role.id, module=p.module, action=p.action))
    await db.commit()
    await db.refresh(role, attribute_names=["permissions"])
    return _role_out(role)


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: str,
    user: Employee = Depends(require("roles", "delete")),
    db: AsyncSession = Depends(get_db),
):
    role = await _get_role(role_id, user, db)
    if role.is_system:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System role cannot be deleted")
    assigned = await db.execute(select(Employee.id).where(Employee.role_id == role.id).limit(1))
    if assigned.first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Role is assigned to users; reassign them first")
    await db.delete(role)
    await db.commit()
