"""Enrollment auto-link: known email links, unknown email provisions a Talent,
existing assignments are never overridden."""
import uuid
import pytest

from app.core.provisioning import (
    humanize_email_name, get_or_create_talent_role, link_or_provision_employee,
    TALENT_ROLE_NAME,
)
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Device, Employee


def test_humanize_email_name():
    assert humanize_email_name("john.doe@corp.com") == "John Doe"
    assert humanize_email_name("jane_smith@x.io") == "Jane Smith"
    assert humanize_email_name("bob@x.io") == "Bob"
    assert humanize_email_name("a-b.c_d@x.io") == "A B C D"


@pytest.mark.asyncio
async def test_link_and_provision_flow():
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"ProvTest-{tag}", enrollment_token=f"ptok-{tag}")
        db.add(company)
        await db.flush()

        # Talent role is created once, then reused.
        role1 = await get_or_create_talent_role(company.id, db)
        role2 = await get_or_create_talent_role(company.id, db)
        assert role1.id == role2.id
        assert role1.name == TALENT_ROLE_NAME
        assert role1.scope_type == "own"
        assert role1.is_system is True

        # Known email → links to the existing employee, no new user.
        known = Employee(company_id=company.id, email=f"known-{tag}@x.io", name="Known")
        dev1 = Device(company_id=company.id, hostname=f"d1-{tag}", platform="darwin")
        db.add_all([known, dev1])
        await db.flush()
        emp = await link_or_provision_employee(dev1, f"  KNOWN-{tag}@X.IO ", db)
        assert emp is not None and emp.id == known.id
        assert dev1.employee_id == known.id

        # Unknown email → auto-provisions an approved, active Talent.
        dev2 = Device(company_id=company.id, hostname=f"d2-{tag}", platform="win32")
        db.add(dev2)
        await db.flush()
        emp2 = await link_or_provision_employee(dev2, f"new.talent@{tag}.example.com", db)
        assert emp2 is not None
        assert emp2.name == "New Talent"
        assert emp2.role_id == role1.id
        assert emp2.is_approved is True and emp2.is_active is True
        assert emp2.password_hash is None
        assert dev2.employee_id == emp2.id

        # Already-assigned device → linking is a no-op (portal wins).
        emp3 = await link_or_provision_employee(dev2, f"other-{tag}@x.io", db)
        assert emp3 is None
        assert dev2.employee_id == emp2.id

        # Garbage email → no-op.
        dev3 = Device(company_id=company.id, hostname=f"d3-{tag}", platform="darwin")
        db.add(dev3)
        await db.flush()
        assert await link_or_provision_employee(dev3, "not-an-email", db) is None
        assert dev3.employee_id is None
        # No commit — rolled back on close.
