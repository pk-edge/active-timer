"""
Remote setup / admin-reset endpoint.

POST /v1/setup/reset-admin
──────────────────────────
Creates or resets the first admin user without needing shell access to the server.
Protected by the SETUP_SECRET env var — the endpoint is disabled (404) when the
secret is blank.

Usage
─────
curl -X POST https://api.tracker.staging.onedge.co/v1/setup/reset-admin \
  -H "X-Setup-Secret: YOUR_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"company":"EDGE","email":"admin@onedge.co","name":"Admin","password":"NewPass123!"}'
"""

import secrets as _secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Company, Employee

router = APIRouter()


class ResetRequest(BaseModel):
    company:  str
    email:    str
    name:     str = "Admin"
    password: str


@router.post("/reset-admin")
async def reset_admin(
    body: ResetRequest,
    x_setup_secret: str = Header(..., alias="X-Setup-Secret"),
    db: AsyncSession = Depends(get_db),
):
    """Create or reset the admin user. Disabled when SETUP_SECRET is blank."""
    settings = get_settings()

    # Endpoint is disabled if no secret is configured
    if not settings.SETUP_SECRET:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Constant-time comparison to prevent timing attacks
    if not _secrets.compare_digest(x_setup_secret, settings.SETUP_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid setup secret",
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
        emp.password_hash = hash_password(body.password)
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
            password_hash = hash_password(body.password),
        )
        db.add(emp)
        action = "created"

    await db.commit()

    return {
        "status":            "ok",
        "action":            action,
        "email":             email,
        "company":           company.name,
        "enrollment_token":  company.enrollment_token,
    }
