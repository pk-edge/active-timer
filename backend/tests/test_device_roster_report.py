"""Device-roster report: row shaping and visibility scoping.

The roster is an inventory snapshot (who owns what, which OS, online state) with
no activity metrics, so the things worth pinning down are the platform labelling,
the status derivation, and that a scoped user cannot export devices outside their
scope. DB rows are created in a never-committed session and rolled back.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints.reports import (
    _fetch_roster_rows,
    _platform_label,
    _roster_result,
)
from app.db.base import AsyncSessionLocal
from app.db.models import Company, Device, Employee


def _row(**kw):
    base = {
        "hostname": "host", "platform": "darwin", "agent_version": "1.0.0",
        "enrolled_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 1, 3, tzinfo=timezone.utc),
        "is_archived": False, "is_online": True, "employee_name": "Someone",
    }
    return SimpleNamespace(**{**base, **kw})


def test_platform_labels_fold_raw_sys_platform():
    assert _platform_label("win32") == "Windows"
    assert _platform_label("darwin") == "macOS"
    assert _platform_label("Linux") == "Linux"
    assert _platform_label("freebsd") == "Other"
    assert _platform_label(None) == "Other"


def test_roster_result_counts_and_shaping():
    result = _roster_result([
        _row(hostname="mac-1", platform="darwin", is_online=True),
        _row(hostname="win-1", platform="win32", is_online=False),
        _row(hostname="win-2", platform="win32", is_online=False, is_archived=True),
        _row(hostname="bsd-1", platform="freebsd", is_online=False, employee_name=None,
             last_seen=None, agent_version=None),
    ])

    assert result["total_devices"] == 4
    assert result["online_devices"] == 1
    assert result["offline_devices"] == 2
    assert result["archived_devices"] == 1
    assert result["unassigned_devices"] == 1

    # Archived wins over online/offline so a device is counted exactly once.
    by_host = {d["hostname"]: d for d in result["devices"]}
    assert by_host["mac-1"]["status"] == "online"
    assert by_host["win-1"]["status"] == "offline"
    assert by_host["win-2"]["status"] == "archived"

    assert by_host["mac-1"]["platform"] == "macOS"
    assert by_host["mac-1"]["raw_platform"] == "darwin"
    assert by_host["bsd-1"]["platform"] == "Other"
    assert by_host["bsd-1"]["last_seen"] is None

    # Breakdown is ordered by count desc, then label.
    assert result["platform_breakdown"][0] == {"platform": "Windows", "count": 2}
    assert {"platform": "macOS", "count": 1} in result["platform_breakdown"]
    assert {"platform": "Other", "count": 1} in result["platform_breakdown"]

    assert sum(p["count"] for p in result["platform_breakdown"]) == result["total_devices"]


def test_roster_result_empty_fleet():
    result = _roster_result([])
    assert result["total_devices"] == 0
    assert result["platform_breakdown"] == []
    assert result["devices"] == []


@pytest.mark.asyncio
async def test_fetch_roster_rows_includes_archived_excludes_deleted_and_honours_scope():
    async with AsyncSessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        company = Company(name=f"Roster-{tag}", enrollment_token=f"tok-{tag}")
        db.add(company)
        await db.flush()

        emp = Employee(company_id=company.id, email=f"e-{tag}@x.io", name="Owner")
        db.add(emp)
        await db.flush()

        now = datetime.now(timezone.utc)
        online   = Device(company_id=company.id, hostname=f"on-{tag}",   platform="darwin",
                          employee_id=emp.id, last_seen=now)
        offline  = Device(company_id=company.id, hostname=f"off-{tag}",  platform="win32",
                          last_seen=now - timedelta(hours=2))
        archived = Device(company_id=company.id, hostname=f"arch-{tag}", platform="win32",
                          last_seen=now, is_archived=True)
        deleted  = Device(company_id=company.id, hostname=f"del-{tag}",  platform="win32",
                          last_seen=now, is_deleted=True)
        db.add_all([online, offline, archived, deleted])
        await db.flush()

        rows = await _fetch_roster_rows(db, str(company.id), None)
        by_host = {r.hostname: r for r in rows}

        assert set(by_host) == {online.hostname, offline.hostname, archived.hostname}, \
            "roster must include archived devices and exclude soft-deleted ones"
        assert by_host[online.hostname].is_online is True
        assert by_host[offline.hostname].is_online is False
        assert by_host[archived.hostname].is_online is False, "archived is never online"
        assert by_host[online.hostname].employee_name == "Owner"
        assert by_host[offline.hostname].employee_name is None

        # A scoped caller only exports the devices their scope resolved to.
        scoped = await _fetch_roster_rows(db, str(company.id), [str(online.id)])
        assert [r.hostname for r in scoped] == [online.hostname]

        # An empty scope (e.g. an RM with no groups) exports nothing.
        assert await _fetch_roster_rows(db, str(company.id), []) == []

        await db.rollback()
