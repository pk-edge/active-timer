"""
Remote setup / admin-reset endpoint.

POST /v1/setup/reset-admin
──────────────────────────
Creates or resets the admin user without needing shell access to the server.

Two access modes, decided automatically by how many admins already exist:

1. FIRST-RUN BOOTSTRAP (no admin exists yet)
   The endpoint accepts the request WITHOUT any secret. This is the standard
   first-boot pattern — it lets you create the very first admin on a fresh
   deployment where no env vars or shell access are available. The window
   closes automatically the instant the first admin is created.

       curl -X POST https://api.tracker.staging.onedge.co/v1/setup/reset-admin \
         -H "Content-Type: application/json" \
         -d '{"company":"EDGE","email":"admin@onedge.co","name":"Admin","password":"Admin123!"}'

2. LOCKED (one or more admins already exist)
   The endpoint now requires the X-Setup-Secret header, matched against
   SETUP_SECRET. If SETUP_SECRET is unset the endpoint is disabled (every
   reset rejected). This prevents anyone from hijacking the admin once set up.

       curl -X POST .../v1/setup/reset-admin \
         -H "X-Setup-Secret: YOUR_SECRET" \
         -H "Content-Type: application/json" \
         -d '{...}'

GET /v1/setup/status  →  whether bootstrap is still open (no secret needed).
"""

import logging
import secrets as _secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.rate_limit import DistributedLimiter
from app.core.security import hash_password_async
from app.db.models import Company, Employee, Role

log = logging.getLogger("setup")
router = APIRouter()

# This endpoint can create/replace the admin account — cap attempts per IP so
# the X-Setup-Secret can't be brute-forced. Legitimate use is one-shot.
_setup_limiter = DistributedLimiter("setup", max_hits=5, window_secs=15 * 60)


class ResetRequest(BaseModel):
    company:  str
    email:    str
    name:     str = "Admin"
    password: str


async def _admin_count(db: AsyncSession) -> int:
    """Number of employees that can log into the dashboard as an admin.

    Counts BOTH legacy `role == "admin"` rows and RBAC accounts whose role has
    all-data scope (Super Admins are created with role="employee" + a role_id).
    Counting only the legacy column let an RBAC-only deployment report zero
    admins and silently re-open the unauthenticated first-run bootstrap.
    """
    result = await db.execute(
        select(func.count()).select_from(Employee)
        .outerjoin(Role, Employee.role_id == Role.id)
        .where(or_(Employee.role == "admin", Role.scope_type == "all"))
    )
    return int(result.scalar_one())


@router.get("/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    """Report whether first-run bootstrap is still available (no secret needed)."""
    count = await _admin_count(db)
    return {
        "admins_exist":   count > 0,
        "admin_count":    count,
        "bootstrap_open": count == 0,
    }


@router.post("/reset-admin")
async def reset_admin(
    body: ResetRequest,
    request: Request,
    x_setup_secret: Optional[str] = Header(None, alias="X-Setup-Secret"),
    db: AsyncSession = Depends(get_db),
):
    """Create or reset the admin user.

    No secret required on first run (zero admins). Once an admin exists, the
    X-Setup-Secret header is required and matched against SETUP_SECRET only.
    If SETUP_SECRET is unset, the endpoint is disabled (every reset is rejected).
    """
    ip = request.client.host if request.client else "unknown"
    if await _setup_limiter.blocked(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many setup attempts. Try again later.")
    await _setup_limiter.hit(ip)

    settings = get_settings()
    existing_admins = await _admin_count(db)
    bootstrap = existing_admins == 0

    if not bootstrap:
        # Locked mode — an admin already exists, so a valid secret is required.
        # Opt-in only: matched against SETUP_SECRET. If SETUP_SECRET is unset the
        # list is empty, so every request is rejected (endpoint effectively off).
        valid_secrets = [settings.SETUP_SECRET] if settings.SETUP_SECRET else []
        provided = x_setup_secret or ""
        if not any(_secrets.compare_digest(provided, s) for s in valid_secrets):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid setup secret (an admin already exists — secret required)",
            )
    else:
        log.warning(
            "First-run bootstrap: creating initial admin '%s' with no secret "
            "(zero admins currently exist)", body.email,
        )

    if len(body.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters",
        )

    # Get or create company
    result = await db.execute(select(Company).where(Company.name == body.company))
    company = result.scalar_one_or_none()
    if not company:
        import secrets as s
        company = Company(name=body.company, enrollment_token=s.token_hex(24))
        db.add(company)
        await db.flush()

    # Get or create admin employee
    email = body.email.lower()
    result = await db.execute(select(Employee).where(Employee.email == email))
    emp = result.scalar_one_or_none()

    if emp:
        emp.password_hash = await hash_password_async(body.password)
        emp.role          = "admin"
        emp.is_active     = True
        emp.is_approved   = True
        emp.name          = body.name
        action = "updated"
    else:
        emp = Employee(
            company_id    = company.id,
            email         = email,
            name          = body.name,
            role          = "admin",
            is_active     = True,
            is_approved   = True,
            password_hash = await hash_password_async(body.password),
        )
        db.add(emp)
        action = "created"

    await db.commit()

    return {
        "status":            "ok",
        "action":            action,
        "mode":              "bootstrap" if bootstrap else "secret",
        "email":             email,
        "company":           company.name,
        "enrollment_token":  company.enrollment_token,
        "note":              "Bootstrap is now closed — future calls require X-Setup-Secret."
                             if bootstrap else "Admin updated via secret.",
    }
