"""
Shared FastAPI dependencies.

Import from here everywhere — never import from individual endpoint files.
"""

import uuid
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.security import decode_token
from app.core.permissions import all_permissions
from app.db.base import AsyncSessionLocal
from app.db.models import Device, Employee, Role

bearer = HTTPBearer()


# ── Database session ──────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# ── Device auth ───────────────────────────────────────────────────────────────

async def get_current_device(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> Device:
    try:
        payload = decode_token(creds.credentials)
        if payload.get("type") != "device":
            raise ValueError
        device_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token")

    result = await db.execute(
        select(Device).where(
            Device.id        == device_uuid,
            Device.jwt_token == creds.credentials,
            Device.is_active == True,
        )
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device not found or revoked")
    return device


# ── Admin / user auth + RBAC ────────────────────────────────────────────────

def _has_permission(perms: set[tuple[str, str]], module: str, action: str) -> bool:
    return (module, action) in perms


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> Employee:
    """Resolve the authenticated dashboard user and attach effective permissions.

    Sets two transient attributes on the returned Employee:
      _perms      : set[(module, action)] the user is allowed
      _scope_type : 'all' | 'groups' | 'own'
    """
    try:
        payload = decode_token(creds.credentials)
        if payload.get("type") != "admin":
            raise ValueError
        user_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")

    result = await db.execute(
        select(Employee)
        .options(selectinload(Employee.role_obj).selectinload(Role.permissions))
        .where(Employee.id == user_uuid, Employee.is_active == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if user.role_obj:
        user._perms = {(p.module, p.action) for p in user.role_obj.permissions}
        user._scope_type = user.role_obj.scope_type
    elif user.role in ("admin", "manager"):       # legacy fallback (pre-RBAC accounts)
        user._perms = set(all_permissions())
        user._scope_type = "all"
    else:
        user._perms = set()
        user._scope_type = "own"
    return user


# Backwards-compatible alias — existing endpoints depend on this name.
get_current_admin = get_current_user


def require(module: str, action: str):
    """Dependency factory: 403 unless the current user holds (module, action)."""
    async def _dep(user: Employee = Depends(get_current_user)) -> Employee:
        if not _has_permission(getattr(user, "_perms", set()), module, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {module}.{action}",
            )
        return user
    return _dep
