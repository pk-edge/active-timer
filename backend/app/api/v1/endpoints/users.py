"""Users CRUD + role/group/password assignment.

A "user" is an Employee row. Monitored people may have no password/role; dashboard
users (admins, RMs) have a password and an assigned role. Group assignment here
controls which groups a 'groups'-scoped user may VIEW (employee_group_access).
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, func, delete, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require
from app.core import audit
from app.core.config import get_settings
from app.core.group_tree import descendant_group_ids
from app.core.ingest_queue import ingest_queue
from app.core.security import hash_password_async
from app.db.models import Employee, Device, Role, EmployeeGroupAccess, Group, GroupMember
from app.schemas.rbac import UserIn, UserPatch, UserOut, ApproveRequest

router = APIRouter()
settings = get_settings()


async def _device_count(emp_id, db: AsyncSession) -> int:
    r = await db.execute(select(func.count()).select_from(Device).where(Device.employee_id == emp_id))
    return int(r.scalar() or 0)


async def _group_ids(emp_id, db: AsyncSession) -> list[str]:
    r = await db.execute(select(EmployeeGroupAccess.group_id).where(EmployeeGroupAccess.employee_id == emp_id))
    return [str(g[0]) for g in r.all()]


async def _user_out(emp: Employee, db: AsyncSession) -> UserOut:
    return UserOut(
        id=str(emp.id), name=emp.name, email=emp.email,
        role_id=str(emp.role_id) if emp.role_id else None,
        role_name=emp.role_obj.name if emp.role_obj else (emp.role or None),
        scope_type=emp.role_obj.scope_type if emp.role_obj else None,
        is_active=emp.is_active,
        is_approved=getattr(emp, "is_approved", True),
        group_ids=await _group_ids(emp.id, db),
        device_count=await _device_count(emp.id, db),
    )


async def _resolve_role(role_id: str | None, company_id, db: AsyncSession) -> Role | None:
    if not role_id:
        return None
    try:
        rid = uuid.UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role id")
    r = await db.execute(select(Role).where(Role.id == rid, Role.company_id == company_id))
    role = r.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


async def _set_group_access(emp_id, company_id, group_ids: list[str], db: AsyncSession) -> None:
    await db.execute(delete(EmployeeGroupAccess).where(EmployeeGroupAccess.employee_id == emp_id))
    guids = []
    for gid in dict.fromkeys(group_ids):  # dedupe, preserve order
        try:
            guids.append(uuid.UUID(gid))
        except ValueError:
            continue
    if not guids:
        return
    # Validate every id against the company in one query (was one per group).
    valid = (await db.execute(
        select(Group.id).where(Group.id.in_(guids), Group.company_id == company_id)
    )).scalars().all()
    for guid in valid:
        db.add(EmployeeGroupAccess(employee_id=emp_id, group_id=guid))


@router.get("", response_model=list[UserOut])
async def list_users(
    search:  str | None = Query(default=None),
    pending: bool = Query(default=False),
    user: Employee = Depends(require("users", "view")),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Employee).options(selectinload(Employee.role_obj))
        .where(Employee.company_id == user.company_id, Employee.is_deleted == False)  # noqa: E712
        .order_by(Employee.created_at.asc())
    )
    if pending:
        q = q.where(Employee.is_approved == False, Employee.is_active == True)  # noqa: E712
    else:
        q = q.where(Employee.is_approved == True)  # noqa: E712
    if search:
        like = f"%{search.lower()}%"
        q = q.where(func.lower(Employee.name).like(like) | func.lower(Employee.email).like(like))

    # Groups-scoped users only see employees who belong to their assigned groups.
    if getattr(user, "_scope_type", "own") == "groups":
        gid_rows = await db.execute(
            select(EmployeeGroupAccess.group_id)
            .where(EmployeeGroupAccess.employee_id == user.id)
        )
        my_gids = [g[0] for g in gid_rows.all()]
        if not my_gids:
            return []
        expanded = await descendant_group_ids(my_gids, user.company_id, db)
        # Employees who are direct group members in assigned groups + subgroups.
        q = q.where(
            Employee.id.in_(
                select(GroupMember.employee_id)
                .where(
                    GroupMember.group_id.in_(expanded),
                    GroupMember.member_type == "employee",
                    GroupMember.employee_id.isnot(None),
                )
            )
        )

    rows = await db.execute(q)
    employees = rows.scalars().all()
    if not employees:
        return []

    emp_ids = [e.id for e in employees]

    # Batch device counts + group access in 2 queries total (was 2 per user → N+1).
    dc_rows = await db.execute(
        select(Device.employee_id, func.count())
        .where(Device.employee_id.in_(emp_ids))
        .group_by(Device.employee_id)
    )
    device_counts = {eid: int(c) for eid, c in dc_rows.all()}

    ga_rows = await db.execute(
        select(EmployeeGroupAccess.employee_id, EmployeeGroupAccess.group_id)
        .where(EmployeeGroupAccess.employee_id.in_(emp_ids))
    )
    groups_by_emp: dict = {}
    for eid, gid in ga_rows.all():
        groups_by_emp.setdefault(eid, []).append(str(gid))

    return [
        UserOut(
            id=str(e.id), name=e.name, email=e.email,
            role_id=str(e.role_id) if e.role_id else None,
            role_name=e.role_obj.name if e.role_obj else (e.role or None),
            scope_type=e.role_obj.scope_type if e.role_obj else None,
            is_active=e.is_active,
            is_approved=getattr(e, "is_approved", True),
            group_ids=groups_by_emp.get(e.id, []),
            device_count=device_counts.get(e.id, 0),
        )
        for e in employees
    ]


# ── Per-user activity (online status + time spent) ───────────────────────────
# NOTE: these routes MUST stay declared before the /{user_id} routes below,
# otherwise FastAPI matches "activity" as a user_id.

class UserActivityOut(BaseModel):
    id:            str
    name:          str
    email:         str
    role_name:     str | None
    is_active:     bool
    device_count:  int
    is_online:     bool
    last_seen:     str | None   # ISO timestamp of the most recent device heartbeat
    current_app:   str | None   # live, from the in-memory ingest cache
    today_seconds: int          # active time since local midnight (UTC)
    week_seconds:  int          # active time over the trailing 7 days


class UserDeviceActivityOut(BaseModel):
    id:            str
    hostname:      str
    platform:      str
    is_online:     bool
    last_seen:     str | None
    current_app:   str | None
    today_seconds: int
    week_seconds:  int


class UserDayOut(BaseModel):
    date:           str
    active_seconds: int
    idle_seconds:   int


class UserAppOut(BaseModel):
    app_name: str
    seconds:  int


class UserActivityDetailOut(BaseModel):
    user:    UserActivityOut
    devices: list[UserDeviceActivityOut]
    daily:   list[UserDayOut]          # trailing 7 days, newest first
    top_apps: list[UserAppOut]         # trailing 7 days, by active time


async def _visible_employees(user: Employee, db: AsyncSession) -> list[Employee]:
    """Active+approved employees this admin may see (mirrors list_users scoping)."""
    q = (
        select(Employee).options(selectinload(Employee.role_obj))
        .where(
            Employee.company_id == user.company_id,
            Employee.is_deleted == False,   # noqa: E712
            Employee.is_approved == True,   # noqa: E712
        )
        .order_by(Employee.name.asc())
    )
    if getattr(user, "_scope_type", "own") == "groups":
        gid_rows = await db.execute(
            select(EmployeeGroupAccess.group_id)
            .where(EmployeeGroupAccess.employee_id == user.id)
        )
        my_gids = [g[0] for g in gid_rows.all()]
        if not my_gids:
            return []
        expanded = await descendant_group_ids(my_gids, user.company_id, db)
        q = q.where(
            Employee.id.in_(
                select(GroupMember.employee_id).where(
                    GroupMember.group_id.in_(expanded),
                    GroupMember.member_type == "employee",
                    GroupMember.employee_id.isnot(None),
                )
            )
        )
    return list((await db.execute(q)).scalars().all())


def _live_app(device_ids: list[str], cutoff: datetime) -> str | None:
    """Most recent current_app across a user's devices, from the ingest cache.
    Pure in-memory lookup — zero DB cost."""
    best_ts, best_app = None, None
    for did in device_ids:
        cached = ingest_queue.device_cache.get(did)
        if not cached:
            continue
        ts = cached.get("last_seen")
        if ts and ts > cutoff and cached.get("current_app"):
            if best_ts is None or ts > best_ts:
                best_ts, best_app = ts, cached["current_app"]
    return best_app


@router.get("/activity", response_model=list[UserActivityOut])
async def list_user_activity(
    user: Employee = Depends(require("users", "view")),
    db: AsyncSession = Depends(get_db),
):
    """One row per employee with devices, online status, and active time.

    Exactly 3 set-based queries regardless of user count (no N+1):
      1. visible employees   2. their devices   3. event sums grouped by employee
    """
    employees = await _visible_employees(user, db)
    if not employees:
        return []
    emp_ids = [e.id for e in employees]
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)

    # Devices per employee (tiny table — counts + last_seen computed in Python).
    dev_rows = (await db.execute(
        select(Device.id, Device.employee_id, Device.last_seen)
        .where(Device.employee_id.in_(emp_ids))
    )).all()
    devs_by_emp: dict = {}
    for did, eid, seen in dev_rows:
        devs_by_emp.setdefault(eid, []).append((str(did), seen))

    # Active seconds (today + trailing 7 days) for ALL users in one aggregate.
    sums = (await db.execute(
        text("""
            SELECT d.employee_id AS eid,
                   COALESCE(SUM(CASE WHEN ae.event_type = 'window_change'
                                      AND ae.timestamp >= date_trunc('day', NOW())
                                     THEN ae.duration ELSE 0 END), 0) AS today,
                   COALESCE(SUM(CASE WHEN ae.event_type = 'window_change'
                                     THEN ae.duration ELSE 0 END), 0) AS week
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.employee_id = ANY(:ids)
              AND ae.timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY d.employee_id
        """),
        {"ids": emp_ids},
    )).all()
    time_by_emp = {r.eid: (int(r.today), int(r.week)) for r in sums}

    out = []
    for e in employees:
        devices  = devs_by_emp.get(e.id, [])
        dev_ids  = [d[0] for d in devices]
        last_seen = max((d[1] for d in devices if d[1]), default=None)
        today, week = time_by_emp.get(e.id, (0, 0))
        out.append(UserActivityOut(
            id=str(e.id), name=e.name, email=e.email,
            role_name=e.role_obj.name if e.role_obj else (e.role or None),
            is_active=e.is_active,
            device_count=len(devices),
            is_online=bool(last_seen and last_seen > cutoff),
            last_seen=last_seen.isoformat() if last_seen else None,
            current_app=_live_app(dev_ids, cutoff),
            today_seconds=today,
            week_seconds=week,
        ))
    return out


@router.get("/{user_id}/activity", response_model=UserActivityDetailOut)
async def user_activity_detail(
    user_id: str,
    user: Employee = Depends(require("users", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Everything the user detail page needs in ONE request (4 small queries):
    summary, per-device breakdown, 7-day daily trend, top apps."""
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user id")

    emp = (await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(
            Employee.id == uid,
            Employee.company_id == user.company_id,
            Employee.is_deleted == False,  # noqa: E712
        )
    )).scalar_one_or_none()
    if not emp:
        raise HTTPException(status_code=404, detail="User not found")

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.ONLINE_CUTOFF_SECS)

    devices = (await db.execute(
        select(Device).where(Device.employee_id == uid).order_by(Device.hostname)
    )).scalars().all()
    dev_ids = [str(d.id) for d in devices]

    per_device: dict = {}
    daily: list[UserDayOut] = []
    top_apps: list[UserAppOut] = []
    if dev_ids:
        rows = (await db.execute(
            text("""
                SELECT device_id,
                       COALESCE(SUM(CASE WHEN event_type = 'window_change'
                                          AND timestamp >= date_trunc('day', NOW())
                                         THEN duration ELSE 0 END), 0) AS today,
                       COALESCE(SUM(CASE WHEN event_type = 'window_change'
                                         THEN duration ELSE 0 END), 0) AS week
                FROM activity_events
                WHERE device_id = ANY(:ids)
                  AND timestamp >= NOW() - INTERVAL '7 days'
                GROUP BY device_id
            """),
            {"ids": dev_ids},
        )).all()
        per_device = {str(r.device_id): (int(r.today), int(r.week)) for r in rows}

        day_rows = (await db.execute(
            text("""
                SELECT DATE(timestamp) AS day,
                       COALESCE(SUM(CASE WHEN event_type = 'window_change' THEN duration ELSE 0 END), 0) AS active,
                       COALESCE(SUM(CASE WHEN event_type = 'idle_start'    THEN duration ELSE 0 END), 0) AS idle
                FROM activity_events
                WHERE device_id = ANY(:ids)
                  AND timestamp >= NOW() - INTERVAL '7 days'
                GROUP BY day ORDER BY day DESC
            """),
            {"ids": dev_ids},
        )).all()
        daily = [UserDayOut(date=str(r.day), active_seconds=int(r.active), idle_seconds=int(r.idle))
                 for r in day_rows]

        app_rows = (await db.execute(
            text("""
                SELECT normalize_app_name(app_name) AS app_name,
                       SUM(duration) AS seconds
                FROM activity_events
                WHERE device_id = ANY(:ids)
                  AND event_type = 'window_change' AND app_name IS NOT NULL
                  AND timestamp >= NOW() - INTERVAL '7 days'
                GROUP BY normalize_app_name(app_name) ORDER BY seconds DESC LIMIT 8
            """),
            {"ids": dev_ids},
        )).all()
        top_apps = [UserAppOut(app_name=r.app_name, seconds=int(r.seconds)) for r in app_rows]

    dev_out = []
    for d in devices:
        t, w = per_device.get(str(d.id), (0, 0))
        cached = ingest_queue.device_cache.get(str(d.id))
        live = bool(d.last_seen and d.last_seen > cutoff)
        dev_out.append(UserDeviceActivityOut(
            id=str(d.id), hostname=d.hostname, platform=d.platform,
            is_online=live,
            last_seen=d.last_seen.isoformat() if d.last_seen else None,
            current_app=(cached or {}).get("current_app") if live else None,
            today_seconds=t, week_seconds=w,
        ))

    last_seen = max((d.last_seen for d in devices if d.last_seen), default=None)
    summary = UserActivityOut(
        id=str(emp.id), name=emp.name, email=emp.email,
        role_name=emp.role_obj.name if emp.role_obj else (emp.role or None),
        is_active=emp.is_active,
        device_count=len(devices),
        is_online=bool(last_seen and last_seen > cutoff),
        last_seen=last_seen.isoformat() if last_seen else None,
        current_app=_live_app(dev_ids, cutoff),
        today_seconds=sum(v[0] for v in per_device.values()),
        week_seconds=sum(v[1] for v in per_device.values()),
    )
    return UserActivityDetailOut(user=summary, devices=dev_out, daily=daily, top_apps=top_apps)


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserIn,
    user: Employee = Depends(require("users", "create")),
    db: AsyncSession = Depends(get_db),
):
    # Only block on active (non-deleted) duplicates.
    dup = await db.execute(
        select(Employee).where(
            Employee.email == body.email.lower(),
            Employee.is_deleted == False,  # noqa: E712
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")

    role = await _resolve_role(body.role_id, user.company_id, db)

    # If a soft-deleted row exists for this email, resurrect it in place.
    # The DB unique constraint on email prevents inserting a second row.
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
        emp.role_id = role.id if role else None
        emp.password_hash = await hash_password_async(body.password) if body.password else None
        emp.is_active = True
        emp.is_approved = True
        emp.is_deleted = False
        emp.google_sub = None
        await db.execute(delete(EmployeeGroupAccess).where(EmployeeGroupAccess.employee_id == emp.id))
        await db.flush()
    else:
        emp = Employee(
            company_id=user.company_id,
            email=body.email.lower(),
            name=body.name,
            role="employee",
            role_id=role.id if role else None,
            password_hash=await hash_password_async(body.password) if body.password else None,
            is_active=True,
        )
        db.add(emp)
        await db.flush()

    await _set_group_access(emp.id, user.company_id, body.group_ids, db)
    await audit.record(db, user, "user.create", "user", str(emp.id), {"email": emp.email})
    await db.commit()

    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


async def _get_user(user_id: str, company_id, db: AsyncSession) -> Employee:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user id")
    r = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj))
        .where(Employee.id == uid, Employee.company_id == company_id)
    )
    emp = r.scalar_one_or_none()
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return emp


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: str,
    user: Employee = Depends(require("users", "view")),
    db: AsyncSession = Depends(get_db),
):
    return await _user_out(await _get_user(user_id, user.company_id, db), db)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserPatch,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    changed: dict = {}
    if body.name is not None and body.name != emp.name:
        changed["name"] = {"from": emp.name, "to": body.name}
        emp.name = body.name
    if body.role_id is not None:
        role = await _resolve_role(body.role_id, user.company_id, db)
        new_role_id = role.id if role else None
        if new_role_id != emp.role_id:
            changed["role"] = role.name if role else None
        emp.role_id = new_role_id
    if body.password:
        changed["password"] = "changed"
        emp.password_hash = await hash_password_async(body.password)
    if body.is_active is not None and body.is_active != emp.is_active:
        changed["is_active"] = body.is_active
        emp.is_active = body.is_active
    if body.group_ids is not None:
        changed["group_ids"] = body.group_ids
        await _set_group_access(emp.id, user.company_id, body.group_ids, db)
    if changed:
        await audit.record(db, user, "user.update", "user", user_id,
                           {"email": emp.email, "changes": changed})
    await db.commit()

    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


@router.put("/{user_id}/groups", response_model=UserOut)
async def set_user_groups(
    user_id: str,
    group_ids: list[str],
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    await _set_group_access(emp.id, user.company_id, group_ids, db)
    await db.commit()
    return await _user_out(emp, db)


@router.post("/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str,
    body: ApproveRequest,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    emp = await _get_user(user_id, user.company_id, db)
    if emp.is_approved:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already approved")

    role = await _resolve_role(body.role_id, user.company_id, db)
    if not role:
        raise HTTPException(status_code=400, detail="role_id is required to approve a user")

    emp.role_id = role.id
    emp.is_approved = True
    await _set_group_access(emp.id, user.company_id, body.group_ids, db)
    await audit.record(db, user, "user.approve", "user", user_id,
                       {"email": emp.email, "role": role.name})
    await db.commit()

    # Expire the object so lazy="joined" re-fetches role_obj on next access.
    await db.refresh(emp)
    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_user(
    user_id: str,
    user: Employee = Depends(require("users", "delete")),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete: flags is_deleted=True. User disappears from all listings."""
    emp = await _get_user(user_id, user.company_id, db)
    if emp.id == user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You cannot delete yourself")
    emp.is_deleted = True
    emp.is_active = False
    await audit.record(db, user, "user.delete", "user", user_id, {"email": emp.email})
    await db.commit()


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: str,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Temporarily deactivate — admin can re-activate later."""
    emp = await _get_user(user_id, user.company_id, db)
    if emp.id == user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You cannot deactivate yourself")
    emp.is_active = False
    await audit.record(db, user, "user.deactivate", "user", user_id, {"email": emp.email})
    await db.commit()
    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)


@router.post("/{user_id}/activate", response_model=UserOut)
async def activate_user(
    user_id: str,
    user: Employee = Depends(require("users", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Re-activate a previously deactivated user."""
    emp = await _get_user(user_id, user.company_id, db)
    emp.is_active = True
    await audit.record(db, user, "user.activate", "user", user_id, {"email": emp.email})
    await db.commit()
    fresh = await db.execute(
        select(Employee).options(selectinload(Employee.role_obj)).where(Employee.id == emp.id)
    )
    return await _user_out(fresh.scalar_one(), db)
