"""Regression tests for the bootstrap-admin upsert (`app.db.admin.ensure_admin`).

These guard the two login-blocker bugs we hit: a created/repaired admin must
have ALL of admin_login's gates satisfied — role="admin", is_active=True,
is_approved=True, and a hashed password — otherwise the deploy "succeeds" but
the admin can't log in (403 pending-approval / 401 inactive).

Runs against the live Postgres (inside the backend container), like
test_scoping. Everything is created in one session that is never committed, so
it rolls back on exit and never pollutes real data. Single test / one event
loop to avoid asyncpg pool reuse across pytest-asyncio function-scoped loops.
"""
import uuid

import pytest
from sqlalchemy import select

from app.core.security import verify_password
from app.db.admin import ensure_admin
from app.db.base import AsyncSessionLocal
from app.db.models import Employee


@pytest.mark.asyncio
async def test_ensure_admin_satisfies_all_login_gates():
    tag = uuid.uuid4().hex[:8]
    company_name = f"BootstrapTest-{tag}"
    email = f"admin-{tag}@x.io"

    async with AsyncSessionLocal() as db:
        # ── CREATE path ──────────────────────────────────────────────────────
        company, created = await ensure_admin(
            db, company_name=company_name, email=email, name="Admin", password="StrongPass123!"
        )
        assert created is True
        assert company.name == company_name
        assert company.enrollment_token  # company gets a token on create

        emp = (await db.execute(select(Employee).where(Employee.email == email))).scalar_one()
        assert emp.role == "admin"
        assert emp.is_active is True
        assert emp.is_approved is True            # regression: was unset → 403 pending-approval
        assert verify_password("StrongPass123!", emp.password_hash)

        # ── REPAIR path (no password reset) ─────────────────────────────────
        # Simulate a self-registered admin: gates flipped off, role demoted.
        emp.is_active = False
        emp.is_approved = False
        emp.role = "employee"
        await db.flush()

        # Bootstrap call (reset_password omitted → defaults to False).
        company2, created2 = await ensure_admin(
            db, company_name=company_name, email=email, name="Admin", password="NewPass456!"
        )
        assert created2 is False                  # existing employee → repaired, not re-created
        assert company2.id == company.id          # existing company reused, not duplicated

        emp = (await db.execute(select(Employee).where(Employee.email == email))).scalar_one()
        assert emp.role == "admin"                # re-promoted
        assert emp.is_active is True              # re-activated
        assert emp.is_approved is True            # regression: repair must restore approval
        # Password must NOT be silently reset — the admin may have changed it manually.
        assert verify_password("StrongPass123!", emp.password_hash)

        # ── REPAIR path (explicit reset_password=True) ────────────────────────
        # The seedAdmin CLI always passes reset_password=True to explicitly rotate.
        _, _ = await ensure_admin(
            db, company_name=company_name, email=email, name="Admin",
            password="NewPass456!", reset_password=True,
        )
        emp = (await db.execute(select(Employee).where(Employee.email == email))).scalar_one()
        assert verify_password("NewPass456!", emp.password_hash)   # rotated only when explicit

        # ── EMAIL NORMALIZATION ──────────────────────────────────────────────
        # A mixed-case / whitespace email must resolve to the same admin row.
        _, created3 = await ensure_admin(
            db, company_name=company_name, email=f"  ADMIN-{tag}@X.IO  ", name="Admin", password="NewPass456!"
        )
        assert created3 is False                  # normalized to the existing lowercase row

        # No commit → the whole graph rolls back on context exit.
