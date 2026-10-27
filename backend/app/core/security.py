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


def create_impersonation_token(
    target_id: str, company_id: str, act_by: str, act_email: str, jti: str,
) -> str:
    """Mint a Super-Admin impersonation token.

    `type` stays "admin" on purpose: every auth path already accepts that type,
    so the impersonated session flows through the normal dependency and inherits
    the TARGET's permissions and data scope with no parallel auth path to keep in
    sync. The `imp` flag is what makes it special, and `app/api/deps.py` reads it.

    `act_by`/`act_email` carry the real admin so the session can be handed back on
    exit and every audit row written during it can be attributed. `jti` keys the
    Redis registry that makes revocation (i.e. a working Exit button) possible.
    Short-lived by design — see settings.IMPERSONATION_TTL_MINUTES.
    """
    return _encode({
        "sub":        target_id,
        "company_id": company_id,
        "type":       "admin",
        "imp":        True,
        "act_by":     act_by,
        "act_email":  act_email,
        "jti":        jti,
        "exp":        datetime.now(timezone.utc)
                      + timedelta(minutes=settings.IMPERSONATION_TTL_MINUTES),
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
