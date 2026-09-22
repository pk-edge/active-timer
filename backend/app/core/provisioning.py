import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.db.models import Device, Employee, Role, RolePermission

log = logging.getLogger(__name__)

TALENT_ROLE_NAME = "Talent"


def is_talent_role(role: Role | None) -> bool:
    """True when `role` is the auto-provisioned, non-editable Talent role."""
    return bool(role and role.is_system and role.name == TALENT_ROLE_NAME)


class _SystemActor:
    """Audit actor for unauthenticated enrollment: company only, no employee."""
    def __init__(self, company_id):
        self.company_id = company_id
        self.id = None


def humanize_email_name(email: str) -> str:
    """Derive a display name from the email local part: john.doe → John Doe."""
    local = email.split("@", 1)[0]
    for sep in (".", "_", "-"):
        local = local.replace(sep, " ")
    parts = [p for p in local.split() if p]
    return " ".join(p.capitalize() for p in parts) or email


async def get_or_create_talent_role(company_id: uuid.UUID, db: AsyncSession) -> Role:
    role = (await db.execute(
        select(Role).where(
            Role.company_id == company_id,
            Role.name == TALENT_ROLE_NAME,
            Role.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if role:
        return role
    role = Role(
        company_id=company_id, name=TALENT_ROLE_NAME, scope_type="own",
        is_system=True, description="Auto-provisioned from device enrollment",
    )
    db.add(role)
    await db.flush()
    db.add(RolePermission(role_id=role.id, module="home", action="view"))
    log.info("Seeded Talent role for company %s", company_id)
    return role


async def link_or_provision_employee(
    device: Device, employee_email: str, db: AsyncSession,
) -> Employee | None:
    """Link device to the employee matching employee_email, creating a Talent
    employee when none exists. No-op when the device is already assigned or
    the email is unusable. Flushes but does not commit."""
    if device.employee_id is not None:
        return None
    email = (employee_email or "").strip().lower()
    if "@" not in email:
        return None

    actor = _SystemActor(device.company_id)

    emp = (await db.execute(
        select(Employee).where(
            Employee.company_id == device.company_id,
            Employee.email == email,
            Employee.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()

    if not emp:
        role = await get_or_create_talent_role(device.company_id, db)
        emp = Employee(
            company_id=device.company_id,
            email=email,
            name=humanize_email_name(email),
            role="employee",
            role_id=role.id,
            password_hash=None,
            is_active=True,
            is_approved=True,
        )
        db.add(emp)
        await db.flush()
        await audit.record(db, actor, "user.auto_provisioned", "user", str(emp.id),
                           {"email": email, "source": "enroll"})
        log.info("Auto-provisioned Talent %s for device %s", email, device.hostname)

    device.employee_id = emp.id
    await audit.record(db, actor, "device.assigned", "device", str(device.id),
                       {"employee_id": str(emp.id), "source": "enroll"})
    return emp
