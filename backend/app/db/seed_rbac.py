"""Idempotent RBAC seed.

Ensures each company has system roles (Super Admin, Viewer) and links any legacy
admin/manager employees that have no role_id to the Super Admin role. Safe to run
on every startup — guarded on role-name existence.
"""
import logging

from sqlalchemy import select

from app.db.base import AsyncSessionLocal
from app.db.models import Company, Employee, Role, RolePermission
from app.core.permissions import all_permissions

log = logging.getLogger(__name__)


async def run() -> None:
    async with AsyncSessionLocal() as db:
        companies = (await db.execute(select(Company))).scalars().all()
        for company in companies:
            # ── Super Admin ──────────────────────────────────────────────────
            sa = (await db.execute(
                select(Role).where(Role.company_id == company.id, Role.name == "Super Admin")
            )).scalar_one_or_none()
            if not sa:
                sa = Role(
                    company_id=company.id, name="Super Admin", scope_type="all",
                    is_system=True, description="Full access to everything",
                )
                db.add(sa)
                await db.flush()
                for module, action in all_permissions():
                    db.add(RolePermission(role_id=sa.id, module=module, action=action))
                log.info("Seeded Super Admin role for company %s", company.name)

            # ── Viewer (group-scoped, read-only starter role) ────────────────
            viewer = (await db.execute(
                select(Role).where(Role.company_id == company.id, Role.name == "Viewer")
            )).scalar_one_or_none()
            if not viewer:
                viewer = Role(
                    company_id=company.id, name="Viewer", scope_type="groups",
                    is_system=False, description="Read-only, group-scoped",
                )
                db.add(viewer)
                await db.flush()
                for module in ("devices", "logs", "reports"):
                    db.add(RolePermission(role_id=viewer.id, module=module, action="view"))
                log.info("Seeded Viewer role for company %s", company.name)

            # ── Link legacy admins/managers (no role_id) to Super Admin ──────
            legacy = (await db.execute(
                select(Employee).where(
                    Employee.company_id == company.id,
                    Employee.role_id.is_(None),
                    Employee.role.in_(("admin", "manager")),
                )
            )).scalars().all()
            for emp in legacy:
                emp.role_id = sa.id
                log.info("Linked legacy admin %s to Super Admin", emp.email)

        await db.commit()
