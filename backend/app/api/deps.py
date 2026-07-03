"""
Shared FastAPI dependencies.

Import from here everywhere — never import from individual endpoint files.
"""

import asyncio
import uuid
from typing import AsyncGenerator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.security import decode_token
from app.core.permissions import all_permissions
from app.core.config import get_settings
from app.core.device_auth_cache import device_auth_cache
from app.db.base import AsyncSessionLocal
from app.db.models import Device, Employee, Role

bearer = HTTPBearer()
_settings = get_settings()

# Bounds how many COLD ingest-auth lookups (cache misses) may hit the DB at once.
# A cold-start stampede (e.g. 1500 agents reconnecting after a deploy) is funnelled
# through this semaphore so miss traffic can never exhaust the 10-slot HTTP pool —
# it is kept well below pool_size+overflow. Warm tokens never reach here.
_ingest_auth_miss_sem = asyncio.Semaphore(_settings.INGEST_AUTH_MISS_CONCURRENCY)


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


async def get_ingest_device_id(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> str:
    """Auth for the HOT ingest path — returns the device_id, NOT a DB-bound ORM row.

    Steady state touches the DB ZERO times: the (token → device_id) mapping is
    served from the in-process device_auth_cache. Only a cold/expired token does a
    single short DB lookup, and that lookup is:
      • concurrency-bounded (_ingest_auth_miss_sem) so a reconnect stampede can
        never exhaust the HTTP pool, and
      • opened/closed in its own `async with` block so it can never leak a
        connection.
    This is what lets /v1/ingest scale to 1000s of devices without the per-post
    auth SELECT overflowing the shared HTTP pool.
    """
    token = creds.credentials
    try:
        payload = decode_token(token)
        if payload.get("type") != "device":
            raise ValueError
        device_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token")

    cached = device_auth_cache.get(token)
    if cached is not None:
        return cached

    # Cold miss → one short, bounded DB validation, then cache it.
    async with _ingest_auth_miss_sem:
        # Re-check under the semaphore: a sibling miss for the same token may have
        # warmed the cache while we waited for a slot.
        cached = device_auth_cache.get(token)
        if cached is not None:
            return cached
        # Capture the invalidation epoch BEFORE the DB read so that an admin
        # revoke racing this lookup cannot be re-warmed by the set() below.
        seen_epoch = device_auth_cache.epoch()
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Device.id).where(
                    Device.id        == device_uuid,
                    Device.jwt_token == token,
                    Device.is_active == True,
                )
            )
            row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device not found or revoked")

    device_id = str(row[0])
    device_auth_cache.set(token, device_id, seen_epoch=seen_epoch)
    return device_id


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

    # One joined query instead of three round-trips (employee → role → permissions);
    # this dependency runs on every authenticated request, so round-trips dominate.
    result = await db.execute(
        select(Employee)
        .options(joinedload(Employee.role_obj).joinedload(Role.permissions))
        .where(Employee.id == user_uuid, Employee.is_active == True)
    )
    user = result.unique().scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if user.role_obj:
        user._scope_type = user.role_obj.scope_type
        # System roles with scope_type="all" (Super Admin) always get full access —
        # matches the frontend can() shortcut and survives DB permission gaps when new
        # modules are added to permissions.py before the server restarts.
        if user.role_obj.scope_type == "all" and user.role_obj.is_system:
            user._perms = set(all_permissions())
        else:
            user._perms = {(p.module, p.action) for p in user.role_obj.permissions}
    elif user.role in ("admin", "manager"):       # legacy fallback (pre-RBAC accounts)
        user._perms = set(all_permissions())
        user._scope_type = "all"
    else:
        user._perms = set()
        user._scope_type = "own"

    # Detach from the session: a later db.rollback() anywhere in the request
    # (e.g. a cache-read helper swallowing an error) expires every attached
    # instance, and the next user.company_id access would lazy-load outside the
    # greenlet context and 500 with MissingGreenlet. Everything the request
    # needs (scalars + role/permissions) is eagerly loaded above, so a detached
    # copy is always safe to read. Endpoints that mutate the current user must
    # issue an explicit UPDATE instead of relying on ORM dirty-tracking.
    db.expunge(user)
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
