"""End-to-end test of the COMPLETE application against the live stack.

Exercises every subsystem through the real HTTP API in dependency order, the
same way production traffic flows:

  health → enrollment → ingest (async queue) → dashboards/reports →
  RBAC scoping → users lifecycle → groups/CSV import → roles CRUD →
  tech analytics → URL policy → screenshots → installers → audit → auth edges

All rows are created under one uniquely-tagged company that is deleted at the
end of the module (companies cascade to employees/devices/events/etc.), so
repeated runs never pollute the database.

Run inside the backend container with the compose Postgres up:
    docker compose run --rm --no-deps backend python -m pytest tests -v
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sa_text

from app.core.security import hash_password
from app.db.admin import ensure_admin
from app.db.base import AsyncSessionLocal
from app.db.models import Employee

TAG = uuid.uuid4().hex[:8]

# Shared state across the ordered tests in this module.
CTX: dict = {
    "company": f"E2E-{TAG}",
    "admin_email": f"e2e-admin-{TAG}@example.com",
    "admin_password": "AdminPass123!",
    "talent_email": f"talent-{TAG}@example.com",
    "hostname": f"e2e-host-{TAG}",
}

WINDOW_APPS = ["Code", "Chrome", "Slack"]
N_WINDOW, WINDOW_SECS = 6, 300          # 6 × 300s = 1800s active today
URL_HOSTS = [f"e2e0-{TAG}.com", f"e2e1-{TAG}.com"]


def _admin_h() -> dict:
    return {"Authorization": f"Bearer {CTX['admin_token']}"}


def _device_h() -> dict:
    return {"Authorization": f"Bearer {CTX['device_token']}"}


# NOTE: cleanup runs as the FINAL test (test_99) instead of a module-scoped
# async fixture — pytest-asyncio 0.23 would give a module fixture its own event
# loop, and asyncpg pooled connections cannot be shared across loops.


# ── 01 · Platform health + versioned migrations ───────────────────────────────

@pytest.mark.asyncio
async def test_01_health_and_migrations(client, db_session):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    r = await client.get("/health/db")
    assert r.status_code == 200 and r.json()["db"] == "reachable"

    # Versioned migration runner: every file on disk recorded → restarts run
    # zero DDL. A missing name means a migration silently failed on last boot.
    from app.db.base import MIGRATIONS_DIR
    on_disk = {f.name for f in MIGRATIONS_DIR.glob("*.sql")}
    rows = await db_session.execute(sa_text("SELECT filename FROM schema_migrations"))
    applied = {f for (f,) in rows.all()}
    assert on_disk <= applied, f"unapplied migrations: {on_disk - applied}"


# ── 02 · Bootstrap company + admin login ──────────────────────────────────────

@pytest.mark.asyncio
async def test_02_bootstrap_and_admin_login(client):
    async with AsyncSessionLocal() as db:
        company, created = await ensure_admin(
            db, company_name=CTX["company"], email=CTX["admin_email"],
            name="E2E Admin", password=CTX["admin_password"],
        )
        await db.commit()
        CTX["company_id"] = company.id
        CTX["enrollment_token"] = company.enrollment_token
    assert created is True

    # Wrong password → 401 (and must not reveal whether the account exists).
    r = await client.post("/v1/auth/admin/login",
                          json={"email": CTX["admin_email"], "password": "WrongPass1!"})
    assert r.status_code == 401

    r = await client.post("/v1/auth/admin/login",
                          json={"email": CTX["admin_email"], "password": CTX["admin_password"]})
    assert r.status_code == 200, r.text
    CTX["admin_token"] = r.json()["access_token"]
    assert r.json()["role"] == "admin"

    r = await client.get("/v1/auth/me", headers=_admin_h())
    assert r.status_code == 200
    me = r.json()
    assert me["email"] == CTX["admin_email"]
    assert me["scope_type"] == "all"
    perms = {(p["module"], p["action"]) for p in me["permissions"]}
    assert ("users", "view") in perms and ("devices", "view") in perms

    r = await client.get("/v1/setup/status")
    assert r.status_code == 200 and r.json()["admins_exist"] is True


# ── 03 · Device enrollment (+ talent auto-provisioning, upsert) ───────────────

@pytest.mark.asyncio
async def test_03_device_enrollment(client):
    body = {
        "enrollment_token": CTX["enrollment_token"],
        "hostname": CTX["hostname"],
        "platform": "darwin",
        "agent_version": "1.0.0",
        "employee_email": CTX["talent_email"],
    }
    r = await client.post("/v1/auth/enroll", json=body)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["device_id"] and first["jwt_token"]

    # Re-enrollment with the same hostname must upsert, not duplicate.
    r = await client.post("/v1/auth/enroll", json=body)
    assert r.status_code == 200
    again = r.json()
    assert again["device_id"] == first["device_id"]

    CTX["device_id"] = again["device_id"]
    CTX["device_token"] = again["jwt_token"]   # latest token wins; old is revoked

    # Invalid enrollment token must be rejected.
    r = await client.post("/v1/auth/enroll", json={**body, "enrollment_token": "bogus"})
    assert r.status_code == 401

    # Talent employee auto-provisioned, humanized name, owns the device.
    r = await client.get("/v1/users", headers=_admin_h())
    assert r.status_code == 200
    talent = next(u for u in r.json() if u["email"] == CTX["talent_email"])
    assert talent["device_count"] == 1
    assert talent["role_name"] == "Talent"
    CTX["talent_id"] = talent["id"]


# ── 04 · Ingest pipeline (async queue → batched DB writes) ────────────────────

@pytest.mark.asyncio
async def test_04_ingest_pipeline(client):
    from app.core.ingest_queue import ingest_queue

    now = int(datetime.now(timezone.utc).timestamp())
    events = []
    for i in range(N_WINDOW):
        events.append({
            "id": str(uuid.uuid4()), "event_type": "window_change",
            "app_name": WINDOW_APPS[i % len(WINDOW_APPS)],
            "window_title": f"Window {i}", "duration": WINDOW_SECS,
            "timestamp": now - i * 60,
        })
    for i, host in enumerate(URL_HOSTS * 2):
        # Mix int and ISO-8601 timestamps — the endpoint must accept both.
        ts: int | str = (now - 400 - i * 30) if i % 2 == 0 else (
            datetime.fromtimestamp(now - 400 - i * 30, tz=timezone.utc)
            .isoformat().replace("+00:00", "Z"))
        events.append({
            "id": str(uuid.uuid4()), "event_type": "url_visit",
            "app_name": "Chrome", "url": f"https://www.{host}/page/{i}",
            "duration": 10, "timestamp": ts,
        })
    events.append({  # unparseable timestamp → skipped, not a 422 for the batch
        "id": str(uuid.uuid4()), "event_type": "window_change",
        "app_name": "Code", "duration": 5, "timestamp": "garbage",
    })

    await ingest_queue.start()
    try:
        r = await client.post("/v1/ingest", json={"events": events}, headers=_device_h())
        assert r.status_code == 202, r.text
        out = r.json()
        assert out["received"] == N_WINDOW + len(URL_HOSTS) * 2
        assert out["skipped"] == 1

        # Workers flush in batches — poll until everything landed.
        expected = out["received"]
        for _ in range(40):
            r = await client.get(f"/v1/devices/{CTX['device_id']}/timeline?limit=100",
                                 headers=_admin_h())
            if r.status_code == 200 and len(r.json()) >= expected:
                break
            await asyncio.sleep(0.25)
        assert len(r.json()) >= expected, "queued events never reached the DB"
    finally:
        await ingest_queue.stop()

    # Worker pool touched last_seen → device must now report online.
    r = await client.get("/v1/devices", headers=_admin_h())
    assert r.status_code == 200
    dev = next(d for d in r.json() if d["id"] == CTX["device_id"])
    assert dev["hostname"] == CTX["hostname"]
    assert dev["is_online"] is True
    assert dev["employee_name"]  # joined-load of the talent owner

    # Agent baseline endpoint (device JWT): today's active seconds.
    r = await client.get("/v1/home/device-today-active", headers=_device_h())
    assert r.status_code == 200
    assert r.json()["active_seconds_today"] == N_WINDOW * WINDOW_SECS


# ── 05 · Dashboards, reports, logs ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_05_dashboards_and_reports(client):
    r = await client.get("/v1/home/overview?days=7", headers=_admin_h())
    assert r.status_code == 200
    ov = r.json()
    assert ov["total_devices"] == 1
    assert ov["active_today"] == 1
    assert ov["avg_active_seconds_today"] == N_WINDOW * WINDOW_SECS
    assert any(t["active_seconds"] > 0 for t in ov["trend"])

    r = await client.get("/v1/home/realtime", headers=_admin_h())
    assert r.status_code == 200
    row = next(d for d in r.json() if d["device_id"] == CTX["device_id"])
    assert row["status"] == "active"

    r = await client.get("/v1/home/app-usage?days=7", headers=_admin_h())
    assert r.status_code == 200
    assert {a["app_name"] for a in r.json()} >= set(WINDOW_APPS)

    r = await client.get("/v1/home/web-usage?days=7", headers=_admin_h())
    assert r.status_code == 200

    r = await client.get("/v1/home/my-stats", headers=_admin_h())
    assert r.status_code == 200
    assert r.json()["device_count"] == 0  # admin owns no devices

    # Regression: device daily summary previously 500'd (non-f-string SQL).
    r = await client.get(f"/v1/reports/device/{CTX['device_id']}/summary?days=7",
                         headers=_admin_h())
    assert r.status_code == 200, r.text
    day = r.json()[0]
    assert day["active_seconds"] == N_WINDOW * WINDOW_SECS

    r = await client.get("/v1/reports/company/summary?days=7", headers=_admin_h())
    assert r.status_code == 200 and r.json()[0]["device_count"] == 1

    r = await client.get("/v1/reports/company/top-apps?days=7", headers=_admin_h())
    assert r.status_code == 200
    assert {a["app_name"] for a in r.json()} >= set(WINDOW_APPS)

    r = await client.get("/v1/reports/company/top-urls?days=7", headers=_admin_h())
    assert r.status_code == 200
    assert {u["url"] for u in r.json()} >= {f"www.{h}" for h in URL_HOSTS}

    r = await client.get(f"/v1/devices/{CTX['device_id']}/stats?days=7", headers=_admin_h())
    assert r.status_code == 200 and r.json()[0]["active_seconds"] > 0

    r = await client.get("/v1/logs?limit=5", headers=_admin_h())
    assert r.status_code == 200
    logs = r.json()
    assert 0 < len(logs) <= 5
    assert all(l["hostname"] == CTX["hostname"] for l in logs)

    r = await client.get("/v1/logs?event_type=url_visit&limit=50", headers=_admin_h())
    assert r.status_code == 200
    assert r.json() and all(l["event_type"] == "url_visit" for l in r.json())


# ── 06 · RBAC: roles, groups, and data scoping end-to-end ─────────────────────

@pytest.mark.asyncio
async def test_06_rbac_groups_scoping(client):
    perms = [{"module": m, "action": "view"} for m in ("devices", "reports", "home", "logs")]
    r = await client.post("/v1/roles", headers=_admin_h(), json={
        "name": f"RM-{TAG}", "description": "group-scoped reviewer",
        "scope_type": "groups", "permissions": perms,
    })
    assert r.status_code == 201, r.text
    CTX["rm_role_id"] = r.json()["id"]

    r = await client.post("/v1/groups", headers=_admin_h(),
                          json={"name": f"grp-{TAG}", "description": "e2e"})
    assert r.status_code == 201, r.text
    CTX["group_id"] = r.json()["id"]

    # Duplicate group name must 409.
    r = await client.post("/v1/groups", headers=_admin_h(), json={"name": f"grp-{TAG}"})
    assert r.status_code == 409

    # Talent (owner of the device) becomes an employee member of the group.
    r = await client.post(f"/v1/groups/{CTX['group_id']}/members", headers=_admin_h(),
                          json={"member_type": "employee", "employee_id": CTX["talent_id"]})
    assert r.status_code == 201, r.text

    rm_email = f"rm-{TAG}@example.com"
    r = await client.post("/v1/users", headers=_admin_h(), json={
        "name": "E2E RM", "email": rm_email, "password": "RmPass123!",
        "role_id": CTX["rm_role_id"], "group_ids": [CTX["group_id"], CTX["group_id"]],
    })
    assert r.status_code == 201, r.text
    CTX["rm_id"] = r.json()["id"]
    assert r.json()["group_ids"] == [CTX["group_id"]]  # deduped

    r = await client.post("/v1/auth/admin/login",
                          json={"email": rm_email, "password": "RmPass123!"})
    assert r.status_code == 200, r.text
    rm_h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Scoped visibility: exactly the group's device, nothing else.
    r = await client.get("/v1/devices", headers=rm_h)
    assert r.status_code == 200
    assert [d["id"] for d in r.json()] == [CTX["device_id"]]

    r = await client.get("/v1/home/overview", headers=rm_h)
    assert r.status_code == 200 and r.json()["total_devices"] == 1

    r = await client.get("/v1/reports/company/summary?days=7", headers=rm_h)
    assert r.status_code == 200 and r.json()[0]["device_count"] == 1

    r = await client.get("/v1/logs?limit=5", headers=rm_h)
    assert r.status_code == 200 and len(r.json()) > 0

    # Missing permissions must 403.
    for path in ("/v1/users", "/v1/roles", "/v1/audit", "/v1/tech/utilization/summary"):
        r = await client.get(path, headers=rm_h)
        assert r.status_code == 403, f"{path} should be forbidden for RM"


# ── 07 · Users lifecycle: create → patch → approve → deactivate → delete ──────

@pytest.mark.asyncio
async def test_07_users_lifecycle(client):
    email = f"user-{TAG}@example.com"
    r = await client.post("/v1/users", headers=_admin_h(), json={
        "name": "Temp User", "email": email,
        "password": "FirstPass123!", "role_id": CTX["rm_role_id"],
    })
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    # Duplicate email → 409.
    r = await client.post("/v1/users", headers=_admin_h(),
                          json={"name": "Dup", "email": email})
    assert r.status_code == 409

    # Patch: rename + rotate password.
    r = await client.patch(f"/v1/users/{uid}", headers=_admin_h(),
                           json={"name": "Renamed User", "password": "SecondPass123!"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed User"

    r = await client.post("/v1/auth/admin/login", json={"email": email, "password": "FirstPass123!"})
    assert r.status_code == 401, "old password must stop working"
    r = await client.post("/v1/auth/admin/login", json={"email": email, "password": "SecondPass123!"})
    assert r.status_code == 200, "new password must work"

    # Pending registration → notification → approve → notification read.
    pending_email = f"pending-{TAG}@example.com"
    async with AsyncSessionLocal() as db:
        emp = Employee(
            company_id=CTX["company_id"], email=pending_email, name="Pending P",
            role="employee", password_hash=hash_password("Pending123!"),
            is_active=True, is_approved=False,
        )
        db.add(emp)
        await db.commit()
        pending_id = str(emp.id)

    r = await client.post("/v1/auth/admin/login",
                          json={"email": pending_email, "password": "Pending123!"})
    assert r.status_code == 403, "pending account must be blocked at login"

    r = await client.get("/v1/notifications", headers=_admin_h())
    assert r.status_code == 200
    notif = next(n for n in r.json() if n["key"] == f"approval:{pending_id}")
    assert notif["is_read"] is False

    r = await client.post("/v1/notifications/mark-read", headers=_admin_h(),
                          json={"keys": [notif["key"], notif["key"]]})
    assert r.status_code == 204
    r = await client.get("/v1/notifications", headers=_admin_h())
    assert next(n for n in r.json() if n["key"] == f"approval:{pending_id}")["is_read"] is True

    r = await client.get("/v1/users?pending=true", headers=_admin_h())
    assert pending_id in {u["id"] for u in r.json()}

    r = await client.post(f"/v1/users/{pending_id}/approve", headers=_admin_h(),
                          json={"role_id": CTX["rm_role_id"], "group_ids": [CTX["group_id"]]})
    assert r.status_code == 200 and r.json()["is_approved"] is True
    r = await client.post("/v1/auth/admin/login",
                          json={"email": pending_email, "password": "Pending123!"})
    assert r.status_code == 200, "approved account must log in"

    # Deactivate blocks login; activate restores it.
    r = await client.post(f"/v1/users/{uid}/deactivate", headers=_admin_h())
    assert r.status_code == 200 and r.json()["is_active"] is False
    r = await client.post("/v1/auth/admin/login", json={"email": email, "password": "SecondPass123!"})
    assert r.status_code == 401
    r = await client.post(f"/v1/users/{uid}/activate", headers=_admin_h())
    assert r.status_code == 200 and r.json()["is_active"] is True

    # Self-deletion is forbidden; deleting others soft-deletes.
    me = await client.get("/v1/auth/me", headers=_admin_h())
    r = await client.delete(f"/v1/users/{me.json()['id']}", headers=_admin_h())
    assert r.status_code == 409
    r = await client.delete(f"/v1/users/{uid}", headers=_admin_h())
    assert r.status_code == 204
    r = await client.get("/v1/users", headers=_admin_h())
    assert uid not in {u["id"] for u in r.json()}


# ── 08 · Groups: CSV import (batched) + membership listing ────────────────────

@pytest.mark.asyncio
async def test_08_groups_csv_import(client):
    csv_body = (
        "group,identifier,type\n"
        f"csv-{TAG},{CTX['talent_email']},employee\n"
        f"csv-{TAG},{CTX['hostname']},device\n"
        f"csv-{TAG},{CTX['talent_email']},employee\n"      # duplicate → skipped
        f"csv-{TAG},nobody-{TAG}@nowhere.io,employee\n"     # unknown → skipped
    )
    r = await client.post("/v1/groups/import", headers=_admin_h(),
                          files={"file": ("groups.csv", csv_body.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created_groups"] == 1
    assert out["added_members"] == 2
    assert len(out["skipped"]) == 2
    reasons = " | ".join(s["reason"] for s in out["skipped"])
    assert "duplicate membership" in reasons and "not found" in reasons

    r = await client.get("/v1/groups", headers=_admin_h())
    grp = next(g for g in r.json() if g["name"] == f"csv-{TAG}")
    assert grp["member_count"] == 2

    r = await client.get(f"/v1/groups/{grp['id']}/members", headers=_admin_h())
    labels = {m["label"] for m in r.json()}
    assert CTX["hostname"] in labels  # device resolved to hostname label


# ── 09 · Roles CRUD: permission replace, guarded delete ───────────────────────

@pytest.mark.asyncio
async def test_09_roles_crud(client):
    r = await client.post("/v1/roles", headers=_admin_h(), json={
        "name": f"disposable-{TAG}", "scope_type": "own",
        "permissions": [{"module": "home", "action": "view"}],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    # Replace the permission set wholesale (bulk delete + insert).
    r = await client.patch(f"/v1/roles/{rid}", headers=_admin_h(), json={
        "name": f"disposable-{TAG}", "scope_type": "own",
        "permissions": [{"module": "logs", "action": "view"},
                        {"module": "reports", "action": "view"}],
    })
    assert r.status_code == 200, r.text
    got = {(p["module"], p["action"]) for p in r.json()["permissions"]}
    assert got == {("logs", "view"), ("reports", "view")}

    # Invalid permission must be rejected.
    r = await client.patch(f"/v1/roles/{rid}", headers=_admin_h(), json={
        "name": f"disposable-{TAG}", "scope_type": "own",
        "permissions": [{"module": "nope", "action": "fly"}],
    })
    assert r.status_code == 400

    # No restriction: deleting a role still assigned to users succeeds and
    # clears it from those users (role_id → NULL) rather than being blocked.
    r = await client.delete(f"/v1/roles/{CTX['rm_role_id']}", headers=_admin_h())
    assert r.status_code == 204
    r = await client.get("/v1/roles", headers=_admin_h())
    assert CTX["rm_role_id"] not in {x["id"] for x in r.json()}
    # The RM user previously holding that role no longer points at it.
    r = await client.get(f"/v1/users/{CTX['rm_id']}", headers=_admin_h())
    assert r.status_code == 200
    assert r.json().get("role_id") in (None, "")

    r = await client.delete(f"/v1/roles/{rid}", headers=_admin_h())
    assert r.status_code == 204
    r = await client.get("/v1/roles", headers=_admin_h())
    assert rid not in {x["id"] for x in r.json()}


# ── 10 · Tech analytics: categories, rules, utilization, websites ─────────────

@pytest.mark.asyncio
async def test_10_tech_analytics(client):
    r = await client.get("/v1/tech/categories", headers=_admin_h())
    assert r.status_code == 200
    cats = r.json()
    assert sum(1 for c in cats if c["is_system"]) >= 7  # defaults auto-seeded

    system_cat = next(c for c in cats if c["is_system"])
    r = await client.delete(f"/v1/tech/categories/{system_cat['id']}", headers=_admin_h())
    assert r.status_code == 400, "system categories must be undeletable"

    r = await client.post("/v1/tech/categories", headers=_admin_h(),
                          json={"name": f"E2E Cat {TAG}", "color": "#123456"})
    assert r.status_code == 201, r.text
    cat_id = r.json()["id"]

    # "Code" is already claimed by the default Development category → 409.
    r = await client.post(f"/v1/tech/categories/{cat_id}/rules", headers=_admin_h(),
                          json={"app_pattern": "Code"})
    assert r.status_code == 409, "one app pattern may map to only one category"
    r = await client.post(f"/v1/tech/categories/{cat_id}/rules", headers=_admin_h(),
                          json={"app_pattern": f"E2EApp-{TAG}"})
    assert r.status_code == 201, r.text

    r = await client.get("/v1/tech/utilization/summary?days=7", headers=_admin_h())
    assert r.status_code == 200
    s = r.json()
    assert s["total_apps"] >= len(WINDOW_APPS) and s["total_users"] >= 1

    r = await client.get("/v1/tech/utilization/category-allocation?days=7", headers=_admin_h())
    assert r.status_code == 200
    names = {i["category_name"] for i in r.json()}
    # Our ingested apps (Code/Chrome/Slack) match the seeded default rules.
    assert {"Development", "Browser", "Communication"} <= names

    r = await client.get("/v1/tech/utilization/apps?days=7", headers=_admin_h())
    assert r.status_code == 200
    assert {a["app_name"] for a in r.json()} >= set(WINDOW_APPS)

    for ep in ("/v1/tech/utilization/app-trends?days=7",
               "/v1/tech/utilization/adoption?days=7",
               "/v1/tech/uncategorized?days=7",
               "/v1/tech/category-usage?days=7",
               "/v1/tech/apps?days=7"):
        r = await client.get(ep, headers=_admin_h())
        assert r.status_code == 200, f"{ep}: {r.text}"

    # Websites pipeline (regression: '(?:' used to break text() bind parsing).
    r = await client.get("/v1/tech/websites?days=7", headers=_admin_h())
    assert r.status_code == 200, r.text
    hosts = {i["host"] for i in r.json()["items"]}
    assert set(URL_HOSTS) <= hosts, f"expected {URL_HOSTS} in {hosts}"

    for tab in ("titles", "subpages", "users", "usage"):
        r = await client.get(f"/v1/tech/websites/detail?host={URL_HOSTS[0]}&tab={tab}",
                             headers=_admin_h())
        assert r.status_code == 200, f"websites/detail tab={tab}: {r.text}"


# ── 11 · URL-blocking policy: admin CRUD ↔ agent fetch ────────────────────────

@pytest.mark.asyncio
async def test_11_policy_blocked_urls(client):
    pattern = f"blocked-{TAG}.com"
    r = await client.post("/v1/policy/blocked-urls", headers=_admin_h(),
                          json={"pattern": pattern, "target_type": "all", "notes": "e2e"})
    assert r.status_code == 201, r.text
    rule_id = r.json()["id"]

    # Agent (device JWT) sees the active rule.
    r = await client.get("/v1/policy/blocked-urls", headers=_device_h())
    assert r.status_code == 200
    assert pattern in {p["pattern"] for p in r.json()}

    # Detected URLs from ingested traffic, minus blocked patterns.
    r = await client.get("/v1/policy/detected-urls", headers=_admin_h())
    assert r.status_code == 200
    assert set(URL_HOSTS) <= {x["host"] for x in r.json()}

    # Deactivate → agent no longer receives it.
    r = await client.patch(f"/v1/policy/blocked-urls/{rule_id}", headers=_admin_h(),
                           json={"is_active": False})
    assert r.status_code == 200
    r = await client.get("/v1/policy/blocked-urls", headers=_device_h())
    assert pattern not in {p["pattern"] for p in r.json()}

    r = await client.delete(f"/v1/policy/blocked-urls/{rule_id}", headers=_admin_h())
    assert r.status_code == 204
    r = await client.get("/v1/policy/blocked-urls/admin", headers=_admin_h())
    assert rule_id not in {x["id"] for x in r.json()}


# ── 12 · Screenshots: presigned upload is an idempotent upsert ────────────────

@pytest.mark.asyncio
async def test_12_screenshots_upsert_and_list(client):
    ss_id = str(uuid.uuid4())
    now = int(datetime.now(timezone.utc).timestamp())

    r1 = await client.post("/v1/screenshots", headers=_device_h(),
                           json={"screenshot_id": ss_id, "captured_at": now, "file_size": 1000})
    assert r1.status_code == 200, r1.text
    assert r1.json()["upload_url"].startswith("http")

    # Agent retry with the same id must upsert, not duplicate or fail.
    r2 = await client.post("/v1/screenshots", headers=_device_h(),
                           json={"screenshot_id": ss_id, "captured_at": now, "file_size": 2222})
    assert r2.status_code == 200
    assert r2.json()["s3_key"] == r1.json()["s3_key"]

    r = await client.get(f"/v1/screenshots/{CTX['device_id']}", headers=_admin_h())
    assert r.status_code == 200
    shots = r.json()
    assert len(shots) == 1 and shots[0]["file_size"] == 2222


# ── 13 · Installers, agent update-check, company info ─────────────────────────

@pytest.mark.asyncio
async def test_13_installers_updates_company(client):
    r = await client.get("/v1/installers", headers=_admin_h())
    assert r.status_code == 200 and r.json() == []

    r = await client.get("/v1/installers/downloads", headers=_admin_h())
    assert r.status_code == 200 and r.json() == []

    r = await client.get("/v1/updates/check", headers=_device_h())
    assert r.status_code == 200 and r.json()["update_available"] is False

    r = await client.get("/v1/company/info", headers=_admin_h())
    assert r.status_code == 200
    info = r.json()
    assert info["name"] == CTX["company"]
    assert info["enrollment_token"] == CTX["enrollment_token"]


# ── 14 · Audit trail of everything this suite did ─────────────────────────────

@pytest.mark.asyncio
async def test_14_audit_trail(client):
    r = await client.get("/v1/audit?limit=100", headers=_admin_h())
    assert r.status_code == 200
    body = r.json()
    actions = {e["action"] for e in body["entries"]}
    assert body["total"] >= 10
    assert {"user.create", "role.create", "group.create",
            "policy.create", "user.delete"} <= actions

    r = await client.get("/v1/audit?action=user.create", headers=_admin_h())
    assert r.status_code == 200
    assert all(e["action"] == "user.create" for e in r.json()["entries"])
    assert r.json()["total"] >= 2

    r = await client.get(f"/v1/audit?search=grp-{TAG}", headers=_admin_h())
    assert r.status_code == 200 and r.json()["total"] >= 1  # matches JSON metadata

    r = await client.get("/v1/audit/filters", headers=_admin_h())
    assert r.status_code == 200
    assert "user.create" in r.json()["actions"]


# ── 15 · Auth boundaries between token types ──────────────────────────────────

@pytest.mark.asyncio
async def test_15_auth_negative(client):
    r = await client.get("/v1/users", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401

    # Device tokens must not open admin endpoints, and vice versa.
    r = await client.get("/v1/users", headers=_device_h())
    assert r.status_code == 401
    r = await client.post("/v1/ingest", json={"events": []}, headers=_admin_h())
    assert r.status_code == 401
    r = await client.get("/v1/home/device-today-active", headers=_admin_h())
    assert r.status_code == 401

    # Missing credentials entirely.
    r = await client.get("/v1/users")
    assert r.status_code in (401, 403)


# ── 16 · Connection-pool hygiene: SSE must not pin pool connections ──────────

@pytest.mark.asyncio
async def test_16_sse_streams_release_db_connections():
    """Regression for the production pool exhaustion: every open dashboard
    stream used to pin one pooled connection for its whole lifetime, so a
    handful of tabs + the ingest workers exhausted the pool and all requests
    started failing with 'QueuePool limit reached'. The stream handlers must
    hand their pooled connection back BEFORE the long-lived streaming starts.

    (Handlers are invoked directly: httpx's ASGITransport cannot consume a
    never-ending StreamingResponse, but the behaviour under test — the
    connection released by the time the response object is returned — is
    exactly observable here via the engine's pool accounting.)
    """
    from app.api.v1.endpoints.devices import stream_device_status
    from app.api.v1.endpoints.home import stream as home_stream
    from app.db.base import AsyncSessionLocal, engine

    class _AdminStub:
        company_id = CTX["company_id"]
        id = uuid.uuid4()
        _scope_type = "all"

    # ── /v1/home/stream ───────────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SELECT 1"))   # auth queries checked out a connection
        held = engine.pool.checkedout()
        assert held >= 1

        resp = await home_stream(group_id=None, user=_AdminStub(), db=db)
        assert resp.media_type == "text/event-stream"
        assert engine.pool.checkedout() == held - 1, (
            "home /stream must release its pooled connection before streaming"
        )

    # ── /v1/devices/stream ────────────────────────────────────────────────────
    # This handler takes no injected session: it opens a short-lived
    # AsyncSessionLocal for auth only and releases it before constructing the
    # StreamingResponse. So a successful call must pin ZERO net connections —
    # the long-lived stream below holds nothing from the pool.
    baseline = engine.pool.checkedout()
    resp = await stream_device_status(token=CTX["admin_token"])
    assert resp.media_type == "text/event-stream"
    assert engine.pool.checkedout() == baseline, (
        "devices /stream must release its auth connection before streaming"
    )


# ── 99 · Tear down everything this suite committed ────────────────────────────

@pytest.mark.asyncio
async def test_99_cleanup():
    cid = CTX.get("company_id")
    assert cid, "bootstrap test never ran"
    async with AsyncSessionLocal() as db:
        # audit_log has no FK; companies cascades to everything else.
        await db.execute(sa_text("DELETE FROM audit_log WHERE company_id = :cid"),
                         {"cid": str(cid)})
        await db.execute(sa_text("DELETE FROM companies WHERE id = :cid"),
                         {"cid": str(cid)})
        await db.commit()

        left = (await db.execute(
            sa_text("SELECT COUNT(*) FROM employees WHERE company_id = :cid"),
            {"cid": str(cid)},
        )).scalar()
    assert left == 0, "company cascade did not clean up employees"
