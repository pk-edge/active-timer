import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from app.core.config import get_settings

settings = get_settings()


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


# bcrypt burns ~100-300ms of CPU per call; run it off the event loop in request
# handlers or a burst of logins stalls every in-flight request (incl. ingest).

async def hash_password_async(plain: str) -> str:
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, hashed: Optional[str]) -> bool:
    return await asyncio.to_thread(verify_password, plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_device_token(device_id: str, company_id: str) -> str:
    return _encode({
        "sub":        device_id,
        "company_id": company_id,
        "type":       "device",
        "exp":        datetime.now(timezone.utc) + timedelta(days=settings.JWT_DEVICE_EXPIRE_DAYS),
    })


def create_admin_token(user_id: str, role: str, company_id: str) -> str:
    return _encode({
        "sub":        user_id,
        "role":       role,
        "company_id": company_id,
        "type":       "admin",
        "exp":        datetime.now(timezone.utc) + timedelta(hours=settings.JWT_ADMIN_EXPIRE_HOURS),
    })


def decode_token(token: str) -> dict:
    # require exp: every token we mint carries one, so a token WITHOUT it is
    # forged/foreign — without this option such a token would validate forever.
    return jwt.decode(
        token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM],
        options={"require": ["exp"]},
    )


def _encode(payload: dict) -> str:
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
