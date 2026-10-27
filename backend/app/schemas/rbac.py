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


class ImpersonatorOut(BaseModel):
    id: str
    email: Optional[str] = None


class MeOut(BaseModel):
    id: str
    name: str
    email: str
    role_name: Optional[str] = None
    scope_type: str
    permissions: list[PermissionItem]
    # Impersonation. `can_impersonate` is the ONLY signal the dashboard gets that
    # the feature exists — it is false for every non-Super-Admin, so the menu
    # never renders for them.
    can_impersonate: bool = False
    impersonating: bool = False
    impersonator: Optional[ImpersonatorOut] = None


class ImpersonationCandidateOut(BaseModel):
    """Deliberately narrower than UserOut — the picker needs identification only."""
    id: str
    name: str
    email: str
    role_name: Optional[str] = None
    user_kind: str = "team"


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
    user_kind: str = "team"               # "talent" | "team"
    is_active: bool
    is_approved: bool = True
    group_ids: list[str] = []
    device_count: int = 0
    created_at: Optional[str] = None


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
    parent_id: Optional[str] = None


class GroupOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    path: Optional[str] = None
    is_archived: bool = False
    member_count: int = 0
    child_count: int = 0
    created_at: Optional[str] = None


class GroupChildOut(BaseModel):
    id: str
    name: str
    member_count: int = 0
    child_count: int = 0
    is_archived: bool = False


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
    group_id: Optional[str] = None         # populated only when include_subgroups=true
    group_name: Optional[str] = None
    path: Optional[str] = None
    direct: Optional[bool] = None


class GroupDetailOut(GroupOut):
    members: list[MemberOut] = []
    children: list[GroupChildOut] = []
    breadcrumb: list[GroupChildOut] = []


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
    # Distribution config (additive; defaults reproduce today's behavior).
    rollout_percent: int = 100
    mandatory: bool = False
    channel: str = "stable"
    min_supported_version: Optional[str] = None
    expires_at: Optional[str] = None


class InstallerDownloadOut(BaseModel):
    id: str
    installer_id: str
    platform: str
    version: str
    installer_type: str
    downloaded_by_id: str
    downloaded_by_name: str
    downloaded_by_email: str
    downloaded_at: str


# ── Auto-update: distribution config + telemetry ──────────────────────────────

class InstallerDistributionIn(BaseModel):
    rollout_percent: Optional[int] = None
    mandatory: Optional[bool] = None
    channel: Optional[str] = None
    min_supported_version: Optional[str] = None
    expires_at: Optional[str] = None   # ISO8601


class UpdateReportIn(BaseModel):
    target_version: str
    from_version: Optional[str] = None
    status: str
    error_code: Optional[str] = None
    progress: Optional[int] = 0
    platform: Optional[str] = None
    channel: Optional[str] = None


class UpdateRunOut(BaseModel):
    id: str
    device_id: str
    from_version: Optional[str] = None
    target_version: str
    status: str
    error_code: Optional[str] = None
    progress: int
    updated_at: Optional[str] = None
