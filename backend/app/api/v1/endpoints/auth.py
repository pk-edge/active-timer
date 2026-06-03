import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db, get_current_user
from app.core.security import create_device_token, create_admin_token, verify_password
from app.db.models import Company, Device, Employee
from app.schemas.auth import EnrollRequest, EnrollResponse, AdminLoginRequest, AdminLoginResponse
from app.schemas.rbac import MeOut, PermissionItem

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/enroll", response_model=EnrollResponse)
async def enroll_device(body: EnrollRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Company).where(Company.enrollment_token == body.enrollment_token)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid enrollment token")

    # Upsert by (company_id, hostname)
    result = await db.execute(
        select(Device).where(Device.company_id == company.id, Device.hostname == body.hostname)
    )
    device = result.scalar_one_or_none()

    if not device:
        device = Device(
            company_id=company.id,
            hostname=body.hostname,
            platform=body.platform,
            agent_version=body.agent_version,
        )
        db.add(device)
        await db.flush()
    else:
        device.platform      = body.platform
        device.agent_version = body.agent_version
        device.is_active     = True

    token = create_device_token(str(device.id), str(company.id))
    device.jwt_token = token
    await db.commit()

    log.info("Device enrolled: %s (%s)", body.hostname, device.id)
    return EnrollResponse(device_id=str(device.id), jwt_token=token)


@router.post("/admin/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Employee).where(Employee.email == body.email.lower())
    )
    employee = result.scalar_one_or_none()

    # A user may sign in if they are active, have a password set, and hold a
    # dashboard role — either a new RBAC role_id or a legacy admin/manager role.
    has_dashboard_role = employee is not None and (
        employee.role_id is not None or employee.role in ("admin", "manager")
    )
    if (
        not employee
        or not employee.is_active
        or not employee.password_hash
        or not has_dashboard_role
        or not verify_password(body.password, employee.password_hash)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    role_name = employee.role_obj.name if employee.role_obj else employee.role
    token = create_admin_token(str(employee.id), employee.role, str(employee.company_id))
    return AdminLoginResponse(access_token=token, role=role_name, name=employee.name)


@router.get("/me", response_model=MeOut)
async def me(user: Employee = Depends(get_current_user)):
    return MeOut(
        id=str(user.id),
        name=user.name,
        email=user.email,
        role_name=user.role_obj.name if user.role_obj else user.role,
        scope_type=getattr(user, "_scope_type", "own"),
        permissions=[
            PermissionItem(module=m, action=a)
            for (m, a) in sorted(getattr(user, "_perms", set()))
        ],
    )
