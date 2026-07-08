import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.api.deps import get_db, get_current_user
from app.core import audit
from app.core.device_auth_cache import device_auth_cache
from app.core.provisioning import link_or_provision_employee
from app.core.rate_limit import SlidingWindowLimiter
from app.core.security import (
    create_device_token, create_admin_token, hash_password_async, verify_password_async,
)
from app.db.models import Company, Device, Employee, EmployeeGroupAccess
from app.schemas.auth import EnrollRequest, EnrollResponse, AdminLoginRequest, AdminLoginResponse
from app.schemas.rbac import MeOut, PermissionItem, RegisterRequest, RegisterResponse

log = logging.getLogger(__name__)
router = APIRouter()

# Brute-force protection for the two unauthenticated credential endpoints.
# Counts FAILED logins per (ip, email): 10 failures / 15 min → 429.
_login_limiter    = SlidingWindowLimiter(max_hits=10, window_secs=15 * 60)
# Public self-registration: 5 submissions per IP per hour.
_register_limiter = SlidingWindowLimiter(max_hits=5, window_secs=60 * 60)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/enroll", response_model=EnrollResponse)
async def enroll_device(body: EnrollRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Company).where(Company.enrollment_token == body.enrollment_token)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid enrollment token")

    # Upsert by (company_id, hostname).
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
    elif device.is_deleted:
        # Soft-deleted device re-enrolling — restore it as a fresh active device.
        # (A unique constraint on (company_id, hostname) prevents creating a new row.)
        device.is_deleted    = False
        device.is_archived   = False
        device.is_active     = True
        device.platform      = body.platform
        device.agent_version = body.agent_version
    else:
        device.platform      = body.platform
        device.agent_version = body.agent_version
        # Archived devices: refresh the token so the agent has a valid credential,
        # but do NOT reactivate — admin must explicitly restore from archive.
        if not device.is_archived:
            device.is_active = True

    token = create_device_token(str(device.id), str(company.id))
    device.jwt_token = token

    # Auto-link to an employee (creates a Talent when the email is unknown).
    # Linking must never break enrollment, and never overrides the portal.
    if body.employee_email and device.employee_id is None:
        try:
            await link_or_provision_employee(device, body.employee_email, db)
        except Exception as e:
            log.warning("Auto-link failed for %s: %s", body.hostname, e)

    await db.commit()

    # Re-enrollment rotates jwt_token. Drop any cached auth entry for this device
    # so a rotated-out token can't keep validating from cache until its TTL.
    device_auth_cache.invalidate_device(str(device.id))

    log.info("Device enrolled: %s (%s)", body.hostname, device.id)
    return EnrollResponse(device_id=str(device.id), jwt_token=token)


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Public self-registration. Account is pending until an admin approves it."""
    ip = _client_ip(request)
    if _register_limiter.blocked(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many registration attempts. Try again later.")
    _register_limiter.hit(ip)

    # Attach to the single company (single-company deployment).
    company_result = await db.execute(select(Company).limit(1))
    company = company_result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="System not yet set up. Contact an administrator.")

    # Only block on active (non-deleted) duplicates.
    dup = await db.execute(
        select(Employee).where(
            Employee.email == body.email.lower(),
            Employee.is_deleted == False,  # noqa: E712
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="An account with this email already exists.")

    # Resurrect a soft-deleted row rather than inserting a duplicate (unique constraint).
    deleted = await db.execute(
        select(Employee).where(
            Employee.email == body.email.lower(),
            Employee.is_deleted == True,  # noqa: E712
        )
    )
    emp = deleted.scalar_one_or_none()
    if emp:
        emp.name = body.name
        emp.role = "employee"
        emp.role_id = None
        emp.password_hash = await hash_password_async(body.password)
        emp.is_active = True
        emp.is_approved = False
        emp.is_deleted = False
        emp.google_sub = None
        await db.execute(delete(EmployeeGroupAccess).where(EmployeeGroupAccess.employee_id == emp.id))
    else:
        emp = Employee(
            company_id=company.id,
            email=body.email.lower(),
            name=body.name,
            role="employee",
            password_hash=await hash_password_async(body.password),
            is_active=True,
            is_approved=False,
        )
        db.add(emp)
    await db.flush()
    await audit.record(db, emp, "user.register", "user", str(emp.id), {"email": emp.email})
    await db.commit()

    log.info("Self-registration submitted: %s (pending approval)", emp.email)
    return RegisterResponse(
        message="Registration submitted. You will be notified once an admin approves your account."
    )


@router.post("/admin/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    limiter_key = f"{_client_ip(request)}:{body.email.lower()}"
    if _login_limiter.blocked(limiter_key):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many failed logins. Try again in a few minutes.")

    result = await db.execute(
        select(Employee).where(
            Employee.email == body.email.lower(),
            Employee.is_deleted == False,  # noqa: E712
        )
    )
    employee = result.scalar_one_or_none()

    # Step 1 — basic credential check (don't reveal whether the account exists).
    if (
        not employee
        or not employee.is_active
        or not employee.password_hash
        or not await verify_password_async(body.password, employee.password_hash)
    ):
        _login_limiter.hit(limiter_key)   # only failures count toward the block
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    _login_limiter.reset(limiter_key)

    # Step 2 — approval gate (password was correct but account is still pending).
    if not getattr(employee, "is_approved", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account pending admin approval",
        )

    # Step 3 — require a dashboard role.
    # Talent employees never reach here (password_hash is None → blocked at Step 1).
    # This catches any role-less account that somehow set a password without a role assignment.
    has_dashboard_access = (
        employee.role_id is not None or employee.role in ("admin", "manager", "viewer")
    )
    if not has_dashboard_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Portal access restricted. This account is not authorized to access the dashboard. Contact your administrator.",
        )

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
