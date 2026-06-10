"""Shared idempotent admin/company upsert.

Used by both the startup bootstrap (``app.db.base._bootstrap_admin``) and the
``seedAdmin`` CLI script, so the create-or-repair logic lives in exactly one place.
"""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Company, Employee


async def ensure_admin(
    db: AsyncSession, *, company_name: str, email: str, name: str, password: str
) -> tuple[Company, bool]:
    """Create-or-repair the company + admin (role=admin, is_active=True).

    Idempotent. The caller commits. Returns ``(company, created)`` — ``created`` is
    True when the admin was newly inserted (vs an existing one repaired) — so callers
    can read ``company.enrollment_token`` and log create-vs-update.
    """
    company = (
        await db.execute(select(Company).where(Company.name == company_name))
    ).scalar_one_or_none()
    if not company:
        company = Company(name=company_name, enrollment_token=secrets.token_hex(24))
        db.add(company)
        await db.flush()

    email = email.strip().lower()
    emp = (await db.execute(select(Employee).where(Employee.email == email))).scalar_one_or_none()
    created = emp is None
    if emp:
        emp.password_hash = hash_password(password)
        emp.role = "admin"
        emp.is_active = True
        emp.is_approved = True  # clear the pending-approval gate so login works
        emp.name = name
    else:
        db.add(
            Employee(
                company_id=company.id, email=email, name=name,
                role="admin", is_active=True, is_approved=True,
                password_hash=hash_password(password),
            )
        )
    return company, created
