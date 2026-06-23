"""Unit tests for nested group helpers."""
import uuid

import pytest

from app.core.group_tree import build_paths, would_create_cycle
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Group


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
