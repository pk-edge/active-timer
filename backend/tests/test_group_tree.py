"""Unit tests for nested group helpers."""
import uuid

import pytest

from app.core.group_tree import build_paths, devices_for_groups, devices_for_groups_detailed, would_create_cycle
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Device, Employee, Group, GroupMember


@pytest.mark.asyncio
async def test_build_paths():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    paths = build_paths([
        (a, "Engineering", None),
        (b, "Backend", a),
        (c, "API", b),
    ])
    assert paths[a] == "Engineering"
    assert paths[b] == "Engineering / Backend"
    assert paths[c] == "Engineering / Backend / API"


@pytest.mark.asyncio
async def test_would_create_cycle():
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"Cycle-{tag}", enrollment_token=f"tok-{tag}")
        db.add(company)
        await db.flush()
        root = Group(company_id=company.id, name=f"Root-{tag}")
        db.add(root)
        await db.flush()
        mid = Group(company_id=company.id, name=f"Mid-{tag}", parent_id=root.id)
        db.add(mid)
        await db.flush()
        leaf = Group(company_id=company.id, name=f"Leaf-{tag}", parent_id=mid.id)
        db.add(leaf)
        await db.flush()

        assert await would_create_cycle(root.id, leaf.id, company.id, db)
        assert not await would_create_cycle(leaf.id, root.id, company.id, db)

        await db.rollback()


@pytest.mark.asyncio
async def test_devices_for_groups_detailed_maps_device_to_group():
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"DevMap-{tag}", enrollment_token=f"tok-{tag}")
        db.add(company)
        await db.flush()

        group = Group(company_id=company.id, name=f"G-{tag}")
        db.add(group)
        await db.flush()

        emp = Employee(company_id=company.id, email=f"e-{tag}@x.io", name="Emp")
        db.add(emp)
        await db.flush()

        direct_device = Device(company_id=company.id, hostname=f"direct-{tag}", platform="macos")
        emp_device = Device(company_id=company.id, hostname=f"emp-{tag}", platform="macos", employee_id=emp.id)
        db.add_all([direct_device, emp_device])
        await db.flush()

        db.add_all([
            GroupMember(group_id=group.id, member_type="device", device_id=direct_device.id),
            GroupMember(group_id=group.id, member_type="employee", employee_id=emp.id),
        ])
        await db.flush()

        detailed = await devices_for_groups_detailed({group.id}, db)
        assert detailed[direct_device.id] == group.id
        assert detailed[emp_device.id] == group.id

        plain = await devices_for_groups({group.id}, db)
        assert set(plain) == {direct_device.id, emp_device.id}

        await db.rollback()
