"""Pydantic schemas for RBAC, users, groups, and installers."""
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


# ── Permissions / roles ─────────────────────────────────────────────────────

class PermissionItem(BaseModel):
    module: str
    action: str


class RoleIn(BaseModel):
    name: str
    description: Optional[str] = None
    scope_type: str = "all"               # all | groups | own
    permissions: list[PermissionItem] = []


class RoleOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    scope_type: str
    is_system: bool
    permissions: list[PermissionItem]


class MeOut(BaseModel):
    id: str
    name: str
    email: str
    role_name: Optional[str] = None
    scope_type: str
    permissions: list[PermissionItem]


# ── Users ───────────────────────────────────────────────────────────────────

class UserIn(BaseModel):
    name: str
    email: EmailStr
    role_id: Optional[str] = None
    password: Optional[str] = None
    is_monitored: bool = True
    group_ids: list[str] = []             # groups this user may VIEW (scope='groups')


class UserPatch(BaseModel):
    name: Optional[str] = None
    role_id: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    group_ids: Optional[list[str]] = None


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    role_id: Optional[str] = None
    role_name: Optional[str] = None
    scope_type: Optional[str] = None
    is_active: bool
    is_approved: bool = True
    group_ids: list[str] = []
    device_count: int = 0


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def _pw_len(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class RegisterResponse(BaseModel):
    message: str


class ApproveRequest(BaseModel):
    role_id: str
    group_ids: list[str] = []


# ── Groups ──────────────────────────────────────────────────────────────────

class GroupIn(BaseModel):
    name: str
    description: Optional[str] = None


class GroupOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    member_count: int = 0
    created_at: Optional[str] = None


class MemberIn(BaseModel):
    member_type: str                       # 'employee' | 'device'
    employee_id: Optional[str] = None
    device_id: Optional[str] = None


class MemberOut(BaseModel):
    id: str
    member_type: str
    employee_id: Optional[str] = None
    device_id: Optional[str] = None
    label: str                             # employee name or device hostname


class GroupDetailOut(GroupOut):
    members: list[MemberOut] = []


class ImportResult(BaseModel):
    created_groups: int
    added_members: int
    skipped: list[dict] = []


# ── Installers ──────────────────────────────────────────────────────────────

class InstallerOut(BaseModel):
    id: str
    platform: str
    installer_type: str
    version: str
    file_size: int
    sha256: Optional[str] = None
    notes: Optional[str] = None
    is_active: bool
    uploaded_at: Optional[str] = None
