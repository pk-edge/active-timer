"""Security-critical: an RM scoped to a group sees ONLY that group's devices.

Runs against the live Postgres (inside the backend container). All rows are
created inside a single transaction that is rolled back at the end, so the test
never pollutes real data. Kept as a single test/one event loop to avoid
asyncpg connection-pool reuse across pytest-asyncio function-scoped loops.
"""
import uuid
import pytest

from app.db.base import AsyncSessionLocal
from app.db.models import Company, Employee, Device, Group, GroupMember, EmployeeGroupAccess
from app.core.scoping import visible_device_ids, ALL


class _ScopedUser:
    """Minimal stand-in carrying the attributes visible_device_ids reads."""
    def __init__(self, employee_id, scope_type):
        self.id = employee_id
        self._scope_type = scope_type


@pytest.mark.asyncio
async def test_scoping_rules():
    async with AsyncSessionLocal() as db:
        # 'all' scope → sentinel (admins skip device filtering).
        assert await visible_device_ids(_ScopedUser(uuid.uuid4(), "all"), db) is ALL

        # 'groups' scope with no access → sees nothing.
        assert await visible_device_ids(_ScopedUser(uuid.uuid4(), "groups"), db) == []

        # Build an isolated fixture graph.
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"ScopeTest-{tag}", enrollment_token=f"tok-{tag}")
        db.add(company)
        await db.flush()

        group_a = Group(company_id=company.id, name=f"A-{tag}")
        group_b = Group(company_id=company.id, name=f"B-{tag}")
        db.add_all([group_a, group_b])
        await db.flush()

        emp_a = Employee(company_id=company.id, email=f"a-{tag}@x.io", name="EmpA")
        emp_b = Employee(company_id=company.id, email=f"b-{tag}@x.io", name="EmpB")
        db.add_all([emp_a, emp_b])
        await db.flush()

        # Group A: one direct device member + one employee member who owns a device.
        dev_a1 = Device(company_id=company.id, hostname=f"a1-{tag}", platform="macos")
        dev_a2 = Device(company_id=company.id, hostname=f"a2-{tag}", platform="macos", employee_id=emp_a.id)
        dev_b1 = Device(company_id=company.id, hostname=f"b1-{tag}", platform="windows", employee_id=emp_b.id)
        db.add_all([dev_a1, dev_a2, dev_b1])
        await db.flush()

        db.add_all([
            GroupMember(group_id=group_a.id, member_type="device", device_id=dev_a1.id),
            GroupMember(group_id=group_a.id, member_type="employee", employee_id=emp_a.id),
            GroupMember(group_id=group_b.id, member_type="device", device_id=dev_b1.id),
        ])
        await db.flush()

        rm = Employee(company_id=company.id, email=f"rm-{tag}@x.io", name="RM")
        db.add(rm)
        await db.flush()
        db.add(EmployeeGroupAccess(employee_id=rm.id, group_id=group_a.id))
        await db.flush()

        ids = await visible_device_ids(_ScopedUser(rm.id, "groups"), db)
        assert ids is not ALL
        visible = set(ids)
        assert dev_a1.id in visible, "direct device member missing"
        assert dev_a2.id in visible, "device of employee member missing"
        assert dev_b1.id not in visible, "LEAK: RM can see another group's device"
        assert visible == {dev_a1.id, dev_a2.id}

        await db.rollback()  # discard all test rows
