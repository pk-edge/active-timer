"""Idempotent RBAC seed.

Ensures each company has system roles (Super Admin, Viewer) and links any legacy
admin/manager employees that have no role_id to the Super Admin role. Safe to run
on every startup — guarded on role-name existence.

All lookups are batched across companies (constant query count) so startup cost
does not grow with the number of companies.
"""
import logging

from sqlalchemy import select

from app.db.base import AsyncSessionLocal
from app.db.models import Company, Employee, Role, RolePermission
from app.core.permissions import all_permissions
from app.core.provisioning import TALENT_ROLE_NAME

log = logging.getLogger(__name__)

_ROLE_DEFS = {
    "Super Admin": dict(scope_type="all", is_system=True,
                        description="Full access to everything"),
    "Viewer": dict(scope_type="groups", is_system=False,
                   description="Read-only, group-scoped"),
    TALENT_ROLE_NAME: dict(scope_type="own", is_system=True,
                           description="Auto-provisioned from device enrollment"),
}

_VIEWER_PERMS = [(m, "view") for m in ("devices", "logs", "reports")]
_TALENT_PERMS = [("home", "view")]


async def run() -> None:
    async with AsyncSessionLocal() as db:
        companies = (await db.execute(select(Company.id, Company.name))).all()
        if not companies:
            return
        company_ids = [c.id for c in companies]

        roles = (await db.execute(
            select(Role).where(
                Role.company_id.in_(company_ids),
                Role.name.in_(tuple(_ROLE_DEFS)),
                Role.is_deleted == False,  # noqa: E712
            )
        )).scalars().all()
        by_key: dict[tuple, Role] = {(r.company_id, r.name): r for r in roles}

        created: set[tuple] = set()
        for cid, cname in companies:
            for name, attrs in _ROLE_DEFS.items():
                if (cid, name) not in by_key:
                    r = Role(company_id=cid, name=name, **attrs)
                    db.add(r)
                    by_key[(cid, name)] = r
                    created.add((cid, name))
                    log.info("Seeded %s role for company %s", name, cname)
        if created:
            await db.flush()  # assign ids to the new roles

        # Existing permissions for every seeded role, fetched in one query.
        role_ids = [r.id for r in by_key.values()]
        have = {
            (rid, m, a) for rid, m, a in (await db.execute(
                select(RolePermission.role_id, RolePermission.module, RolePermission.action)
                .where(RolePermission.role_id.in_(role_ids))
            )).all()
        }

        for (cid, name), role in by_key.items():
            if name == "Super Admin":
                # Always backfill — covers modules added in upgrades.
                wanted = all_permissions()
            elif (cid, name) in created:
                # Viewer is admin-editable, Talent is fixed; either way only the
                # starter permissions are seeded, and only at creation time.
                wanted = _VIEWER_PERMS if name == "Viewer" else _TALENT_PERMS
            else:
                continue
            for module, action in wanted:
                if (role.id, module, action) not in have:
                    db.add(RolePermission(role_id=role.id, module=module, action=action))

        # ── Link legacy admins/managers (no role_id) to Super Admin ──────────
        legacy = (await db.execute(
            select(Employee).where(
                Employee.company_id.in_(company_ids),
                Employee.role_id.is_(None),
                Employee.role.in_(("admin", "manager")),
            )
        )).scalars().all()
        for emp in legacy:
            emp.role_id = by_key[(emp.company_id, "Super Admin")].id
            log.info("Linked legacy admin %s to Super Admin", emp.email)

        await db.commit()
