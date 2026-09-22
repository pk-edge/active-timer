"""Super-Admin impersonation: gates, token lifecycle, and audit attribution.

Security-critical, so the negative cases carry as much weight as the happy path:
who is refused, who cannot be targeted, and what a revoked token can still do.

Redis is stubbed with an in-memory fake rather than mocked away, because the
revocation registry IS the feature — a test that skips it would pass while the
Exit button did nothing.
"""
import uuid

import pytest

from app.core import impersonation
from app.core.security import create_admin_token, create_impersonation_token, decode_token
from app.db.models import Company, Employee, Role, RolePermission, AuditLog
from sqlalchemy import select, update


# ── Fake Redis ────────────────────────────────────────────────────────────────

class _FakeRedis:
    """Minimal stand-in for the two calls the registry makes."""
    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    async def setex(self, key, ttl, value):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value

    async def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def delete(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    # Patch at both call sites: the registry, and the SSE endpoint's own check.
    monkeypatch.setattr(impersonation, "get_client", lambda: r)
    return r


# ── Fixture helpers ───────────────────────────────────────────────────────────

async def _company(db):
    tag = uuid.uuid4().hex[:8]
    c = Company(name=f"Imp-{tag}", enrollment_token=f"tok-{tag}")
    db.add(c)
    await db.flush()
    return c, tag


async def _role(db, company, name, *, scope_type="all", is_system=False, perms=()):
    r = Role(company_id=company.id, name=name, scope_type=scope_type, is_system=is_system)
    db.add(r)
    await db.flush()
    for module, action in perms:
        db.add(RolePermission(role_id=r.id, module=module, action=action))
    await db.flush()
    return r


async def _employee(db, company, role, label, *, is_active=True):
    tag = uuid.uuid4().hex[:6]
    e = Employee(
        company_id=company.id, email=f"{label}-{tag}@test.io", name=f"{label} {tag}",
        role_id=role.id if role else None, is_active=is_active, is_approved=True,
        password_hash="x",
    )
    db.add(e)
    await db.flush()
    return e


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(emp, company):
    return _auth(create_admin_token(str(emp.id), emp.role, str(company.id)))


# ── Who may start a session ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_non_super_admin_cannot_see_or_start_impersonation(client, db_session, fake_redis):
    """A plain admin with full users permissions still gets 403 — and the 403 is
    generic, so the feature isn't discoverable."""
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    plain_role = await _role(
        db_session, company, "Ops",
        perms=[("users", "view"), ("users", "edit"), ("users", "delete")],
    )
    target = await _employee(db_session, company, None, "target")
    plain = await _employee(db_session, company, plain_role, "ops")
    await db_session.flush()

    headers = _admin_headers(plain, company)

    r = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)}, headers=headers)
    assert r.status_code == 403
    assert "impersonat" not in r.json()["detail"].lower(), "403 must not reveal the feature"

    r = await client.get("/v1/users/impersonatable", headers=headers)
    assert r.status_code == 403

    r = await client.get("/v1/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["can_impersonate"] is False

    await db_session.rollback()


@pytest.mark.asyncio
async def test_super_admin_sees_can_impersonate(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    await db_session.flush()

    r = await client.get("/v1/auth/me", headers=_admin_headers(admin, company))
    assert r.status_code == 200
    body = r.json()
    assert body["can_impersonate"] is True
    assert body["impersonating"] is False
    assert body["impersonator"] is None

    await db_session.rollback()


@pytest.mark.asyncio
async def test_custom_all_scope_role_is_not_a_super_admin(client, db_session, fake_redis):
    """scope_type='all' alone must not unlock impersonation — is_system is the
    part that cannot be granted through the API."""
    company, _ = await _company(db_session)
    sneaky = await _role(db_session, company, "Pretend Admin", scope_type="all", is_system=False,
                         perms=[("users", "view")])
    impostor = await _employee(db_session, company, sneaky, "impostor")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    r = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                          headers=_admin_headers(impostor, company))
    assert r.status_code == 403

    await db_session.rollback()


# ── Who may be targeted ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_target_guards(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    other_company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    other_sa_role = await _role(db_session, other_company, "Super Admin", is_system=True)

    admin = await _employee(db_session, company, sa_role, "admin")
    peer_admin = await _employee(db_session, company, sa_role, "peer")
    inactive = await _employee(db_session, company, None, "inactive", is_active=False)
    foreign = await _employee(db_session, other_company, other_sa_role, "foreign")
    await db_session.flush()

    headers = _admin_headers(admin, company)

    # Self
    r = await client.post("/v1/auth/impersonate", json={"user_id": str(admin.id)}, headers=headers)
    assert r.status_code == 409

    # Another Super Admin
    r = await client.post("/v1/auth/impersonate", json={"user_id": str(peer_admin.id)}, headers=headers)
    assert r.status_code == 403

    # Deactivated
    r = await client.post("/v1/auth/impersonate", json={"user_id": str(inactive.id)}, headers=headers)
    assert r.status_code == 409

    # Cross-tenant — must be indistinguishable from "doesn't exist"
    r = await client.post("/v1/auth/impersonate", json={"user_id": str(foreign.id)}, headers=headers)
    assert r.status_code == 404

    # Malformed
    r = await client.post("/v1/auth/impersonate", json={"user_id": "not-a-uuid"}, headers=headers)
    assert r.status_code == 400

    await db_session.rollback()


@pytest.mark.asyncio
async def test_picker_excludes_super_admins_self_and_inactive(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    peer = await _employee(db_session, company, sa_role, "peer")
    ok = await _employee(db_session, company, None, "pickable")
    inactive = await _employee(db_session, company, None, "inactive", is_active=False)
    await db_session.flush()

    r = await client.get("/v1/users/impersonatable", headers=_admin_headers(admin, company))
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}

    assert str(ok.id) in ids
    assert str(admin.id) not in ids, "self must not be offered"
    assert str(peer.id) not in ids, "another Super Admin must not be offered"
    assert str(inactive.id) not in ids, "deactivated user must not be offered"

    await db_session.rollback()


# ── Token lifecycle ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_issues_scoped_token_and_registers_session(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    viewer = await _role(db_session, company, "Viewer", scope_type="groups",
                         perms=[("devices", "view")])
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, viewer, "target")
    await db_session.flush()

    r = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                          headers=_admin_headers(admin, company))
    assert r.status_code == 200
    body = r.json()
    assert body["target"]["id"] == str(target.id)
    assert body["expires_in"] == 30 * 60

    claims = decode_token(body["access_token"])
    assert claims["sub"] == str(target.id), "token must act as the TARGET"
    assert claims["imp"] is True
    assert claims["act_by"] == str(admin.id)
    assert claims["act_email"] == admin.email
    assert impersonation.session_key(claims["jti"]) in fake_redis.store

    # The impersonated session really is the target: their permissions, not the admin's.
    me = await client.get("/v1/auth/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["id"] == str(target.id)
    assert me_body["scope_type"] == "groups"
    assert me_body["impersonating"] is True
    assert me_body["impersonator"]["id"] == str(admin.id)
    assert me_body["can_impersonate"] is False, "no impersonation chaining"
    assert {"module": "devices", "action": "view"} in me_body["permissions"]
    assert {"module": "users", "action": "delete"} not in me_body["permissions"]

    await db_session.rollback()


@pytest.mark.asyncio
async def test_start_refuses_when_registry_unavailable(client, db_session, fake_redis):
    """No revocation registry means no revocable session — refuse rather than
    issue a token whose Exit button would be cosmetic."""
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    fake_redis.fail = True
    r = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                          headers=_admin_headers(admin, company))
    assert r.status_code == 503

    await db_session.rollback()


@pytest.mark.asyncio
async def test_exit_revokes_session_and_returns_fresh_admin_token(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    start = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                              headers=_admin_headers(admin, company))
    imp_token = start.json()["access_token"]
    jti = decode_token(imp_token)["jti"]

    r = await client.post("/v1/auth/impersonate/exit", headers=_auth(imp_token))
    assert r.status_code == 200
    restored = r.json()["access_token"]

    # Session is really gone from the registry...
    assert impersonation.session_key(jti) not in fake_redis.store

    # ...and the revoked token no longer authenticates, even though it hasn't expired.
    dead = await client.get("/v1/auth/me", headers=_auth(imp_token))
    assert dead.status_code == 401

    # The handback is an ordinary admin token for the real admin.
    restored_claims = decode_token(restored)
    assert restored_claims["sub"] == str(admin.id)
    assert "imp" not in restored_claims

    me = await client.get("/v1/auth/me", headers=_auth(restored))
    assert me.status_code == 200
    assert me.json()["id"] == str(admin.id)
    assert me.json()["impersonating"] is False
    assert me.json()["can_impersonate"] is True

    await db_session.rollback()


@pytest.mark.asyncio
async def test_exit_refuses_handback_when_admin_no_longer_eligible(client, db_session, fake_redis):
    """Admin deactivated mid-session: the session still ends, but no token back."""
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    start = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                              headers=_admin_headers(admin, company))
    imp_token = start.json()["access_token"]
    jti = decode_token(imp_token)["jti"]

    # Explicit UPDATE, not `admin.is_active = False`: get_current_user expunged
    # this instance during the start request, so mutating the object would never
    # reach the database (see the detach note in app/api/deps.py).
    await db_session.execute(
        update(Employee).where(Employee.id == admin.id).values(is_active=False)
    )
    await db_session.flush()

    r = await client.post("/v1/auth/impersonate/exit", headers=_auth(imp_token))
    assert r.status_code == 403
    assert impersonation.session_key(jti) not in fake_redis.store, \
        "session must end even when the handback is refused"

    await db_session.rollback()


@pytest.mark.asyncio
async def test_exit_rejects_an_ordinary_admin_token(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    await db_session.flush()

    r = await client.post("/v1/auth/impersonate/exit", headers=_admin_headers(admin, company))
    assert r.status_code == 400

    await db_session.rollback()


@pytest.mark.asyncio
async def test_forged_impersonation_token_without_registry_entry_is_rejected(
    client, db_session, fake_redis,
):
    """A correctly-signed token is not enough — it must match a live session."""
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    token = create_impersonation_token(
        target_id=str(target.id), company_id=str(company.id),
        act_by=str(admin.id), act_email=admin.email, jti=str(uuid.uuid4()),
    )
    r = await client.get("/v1/auth/me", headers=_auth(token))
    assert r.status_code == 401

    await db_session.rollback()


# ── Blast radius ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_impersonated_session_cannot_change_credentials_or_roles(
    client, db_session, fake_redis,
):
    """Impersonation is for operating as someone, never for taking their account
    or escalating out of the session."""
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    editor = await _role(db_session, company, "People Ops",
                         perms=[("users", "view"), ("users", "edit")])
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, editor, "target")
    victim = await _employee(db_session, company, None, "victim")
    await db_session.flush()

    start = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                              headers=_admin_headers(admin, company))
    imp = _auth(start.json()["access_token"])

    # Cannot set their own password (account takeover)
    r = await client.post("/v1/auth/set-password", json={"password": "hunter2hunter2"}, headers=imp)
    assert r.status_code == 403

    # Cannot change anyone's password via the users PATCH route
    r = await client.patch(f"/v1/users/{victim.id}", json={"password": "hunter2hunter2"}, headers=imp)
    assert r.status_code == 403

    # Cannot grant Super Admin to escape the session
    r = await client.patch(f"/v1/users/{victim.id}", json={"role_id": str(sa_role.id)}, headers=imp)
    assert r.status_code == 403

    # A benign edit the target is genuinely allowed still works.
    r = await client.patch(f"/v1/users/{victim.id}", json={"name": "Renamed By Session"}, headers=imp)
    assert r.status_code == 200

    await db_session.rollback()


# ── Audit ─────────────────────────────────────────────────────────────────────

async def _audit_rows(db, company_id, action=None):
    q = select(AuditLog).where(AuditLog.company_id == company_id)
    if action:
        q = q.where(AuditLog.action == action)
    return (await db.execute(q.order_by(AuditLog.created_at.asc()))).scalars().all()


@pytest.mark.asyncio
async def test_start_and_stop_are_audited(client, db_session, fake_redis):
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, None, "target")
    await db_session.flush()

    start = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                              headers=_admin_headers(admin, company))
    imp_token = start.json()["access_token"]
    await client.post("/v1/auth/impersonate/exit", headers=_auth(imp_token))

    started = await _audit_rows(db_session, company.id, "user.impersonate.start")
    stopped = await _audit_rows(db_session, company.id, "user.impersonate.stop")

    assert len(started) == 1
    assert started[0].actor_employee_id == admin.id
    assert started[0].target_id == str(target.id)
    assert started[0].audit_metadata["target_email"] == target.email

    assert len(stopped) == 1
    assert stopped[0].impersonator_id == admin.id

    await db_session.rollback()


@pytest.mark.asyncio
async def test_actions_during_impersonation_carry_the_impersonator(client, db_session, fake_redis):
    """The whole point of the audit columns: an action taken while wearing
    someone's identity must not look like they took it themselves."""
    company, _ = await _company(db_session)
    sa_role = await _role(db_session, company, "Super Admin", is_system=True)
    editor = await _role(db_session, company, "People Ops",
                         perms=[("users", "view"), ("users", "edit")])
    admin = await _employee(db_session, company, sa_role, "admin")
    target = await _employee(db_session, company, editor, "target")
    victim = await _employee(db_session, company, None, "victim")
    await db_session.flush()

    start = await client.post("/v1/auth/impersonate", json={"user_id": str(target.id)},
                              headers=_admin_headers(admin, company))
    imp = _auth(start.json()["access_token"])

    r = await client.patch(f"/v1/users/{victim.id}", json={"name": "Touched"}, headers=imp)
    assert r.status_code == 200

    rows = await _audit_rows(db_session, company.id, "user.update")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_employee_id == target.id, "acting identity is still the target"
    assert row.impersonator_id == admin.id, "but the real driver is recorded"
    assert row.impersonator_email == admin.email

    # And an ordinary action by the admin carries no impersonation stamp.
    r = await client.patch(f"/v1/users/{victim.id}", json={"name": "Touched Directly"},
                           headers=_admin_headers(admin, company))
    assert r.status_code == 200
    rows = await _audit_rows(db_session, company.id, "user.update")
    assert rows[-1].impersonator_id is None

    await db_session.rollback()
