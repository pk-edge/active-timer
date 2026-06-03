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

from app.core.security import decode_token
from app.db.base import AsyncSessionLocal
from app.db.models import Device, Employee

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


# ── Admin auth ────────────────────────────────────────────────────────────────

async def get_current_admin(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> Employee:
    try:
        payload = decode_token(creds.credentials)
        if payload.get("type") != "admin":
            raise ValueError
        user_uuid = uuid.UUID(payload["sub"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")

    result = await db.execute(
        select(Employee).where(
            Employee.id        == user_uuid,
            Employee.is_active == True,
        )
    )
    employee = result.scalar_one_or_none()
    if not employee:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return employee
