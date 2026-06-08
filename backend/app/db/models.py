import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Company(Base):
    __tablename__ = "companies"

    id:               Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    name:             Mapped[str]       = mapped_column(String, nullable=False)
    enrollment_token: Mapped[str]       = mapped_column(String, unique=True, nullable=False)
    created_at:       Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    devices:   Mapped[list["Device"]]   = relationship(back_populates="company")
    employees: Mapped[list["Employee"]] = relationship(back_populates="company")


class Employee(Base):
    __tablename__ = "employees"

    id:            Mapped[uuid.UUID]  = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:    Mapped[uuid.UUID]  = mapped_column(UUID, ForeignKey("companies.id"))
    email:         Mapped[str]        = mapped_column(String, unique=True, nullable=False)
    name:          Mapped[str]        = mapped_column(String, nullable=False)
    role:          Mapped[str]        = mapped_column(String, default="employee")
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active:     Mapped[bool]       = mapped_column(Boolean, default=True)
    is_approved:   Mapped[bool]       = mapped_column(Boolean, default=True)
    is_deleted:    Mapped[bool]       = mapped_column(Boolean, default=False)
    role_id:       Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("roles.id"), nullable=True)
    created_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    company:  Mapped["Company"]        = relationship(back_populates="employees")
    devices:  Mapped[list["Device"]]   = relationship(back_populates="employee")
    role_obj: Mapped["Role | None"]    = relationship(lazy="joined")


class Device(Base):
    __tablename__ = "devices"

    id:            Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:    Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("companies.id"))
    employee_id:   Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    hostname:      Mapped[str]              = mapped_column(String, nullable=False)
    platform:      Mapped[str]              = mapped_column(String, nullable=False)
    agent_version: Mapped[str | None]       = mapped_column(String, nullable=True)
    jwt_token:     Mapped[str | None]       = mapped_column(Text, unique=True, nullable=True)
    enrolled_at:   Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_seen:     Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    is_active:     Mapped[bool]             = mapped_column(Boolean, default=True)

    company:     Mapped["Company"]                   = relationship(back_populates="devices")
    employee:    Mapped["Employee | None"]            = relationship(back_populates="devices", lazy="joined")
    events:      Mapped[list["ActivityEvent"]]        = relationship(back_populates="device")
    screenshots: Mapped[list["Screenshot"]]          = relationship(back_populates="device")


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id:           Mapped[uuid.UUID]     = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    device_id:    Mapped[uuid.UUID]     = mapped_column(UUID, ForeignKey("devices.id"))
    event_type:   Mapped[str]           = mapped_column(String, nullable=False)
    app_name:     Mapped[str | None]    = mapped_column(String, nullable=True)
    window_title: Mapped[str | None]    = mapped_column(Text, nullable=True)
    url:          Mapped[str | None]    = mapped_column(Text, nullable=True)
    duration:     Mapped[int]           = mapped_column(Integer, default=0)
    timestamp:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), nullable=False)
    received_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    device: Mapped["Device"] = relationship(back_populates="events")


class Screenshot(Base):
    __tablename__ = "screenshots"

    id:          Mapped[uuid.UUID]    = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    device_id:   Mapped[uuid.UUID]    = mapped_column(UUID, ForeignKey("devices.id"))
    s3_key:      Mapped[str]          = mapped_column(String, nullable=False)
    captured_at: Mapped[datetime]     = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    file_size:   Mapped[int]          = mapped_column(Integer, default=0)

    device: Mapped["Device"] = relationship(back_populates="screenshots")


class DailySummary(Base):
    __tablename__ = "daily_summaries"

    id:             Mapped[uuid.UUID]    = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    device_id:      Mapped[uuid.UUID]    = mapped_column(UUID, ForeignKey("devices.id"))
    date:           Mapped[datetime]     = mapped_column(Date, nullable=False)
    active_seconds: Mapped[int]          = mapped_column(Integer, default=0)
    idle_seconds:   Mapped[int]          = mapped_column(Integer, default=0)
    top_apps:       Mapped[dict | None]  = mapped_column(JSON, nullable=True)
    top_urls:       Mapped[dict | None]  = mapped_column(JSON, nullable=True)
    session_count:  Mapped[int]          = mapped_column(Integer, default=0)


# ── RBAC ──────────────────────────────────────────────────────────────────────

class Role(Base):
    __tablename__ = "roles"

    id:          Mapped[uuid.UUID]      = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:  Mapped[uuid.UUID]      = mapped_column(UUID, ForeignKey("companies.id"))
    name:        Mapped[str]            = mapped_column(String, nullable=False)
    description: Mapped[str | None]     = mapped_column(Text, nullable=True)
    scope_type:  Mapped[str]            = mapped_column(String, default="all")
    is_system:   Mapped[bool]           = mapped_column(Boolean, default=False)
    is_deleted:  Mapped[bool]           = mapped_column(Boolean, default=False)
    created_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"

    id:      Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("roles.id"))
    module:  Mapped[str]       = mapped_column(String, nullable=False)
    action:  Mapped[str]       = mapped_column(String, nullable=False)

    role:    Mapped["Role"]    = relationship(back_populates="permissions")


class Group(Base):
    __tablename__ = "groups"

    id:          Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:  Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("companies.id"))
    name:        Mapped[str]              = mapped_column(String, nullable=False)
    description: Mapped[str | None]       = mapped_column(Text, nullable=True)
    created_by:  Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    created_at:  Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    is_deleted:  Mapped[bool]             = mapped_column(Boolean, default=False)

    members:     Mapped[list["GroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    id:          Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    group_id:    Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("groups.id"))
    member_type: Mapped[str]              = mapped_column(String, nullable=False)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    device_id:   Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("devices.id"), nullable=True)
    added_at:    Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    group:       Mapped["Group"]          = relationship(back_populates="members")


class EmployeeGroupAccess(Base):
    __tablename__ = "employee_group_access"

    id:          Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("employees.id"))
    group_id:    Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("groups.id"))


class InstallerVersion(Base):
    __tablename__ = "installer_versions"

    id:             Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:     Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("companies.id"))
    platform:       Mapped[str]              = mapped_column(String, nullable=False)
    installer_type: Mapped[str]              = mapped_column(String, nullable=False)
    version:        Mapped[str]              = mapped_column(String, nullable=False)
    s3_key:         Mapped[str]              = mapped_column(Text, nullable=False)
    file_size:      Mapped[int]              = mapped_column(BigInteger, default=0)
    sha256:         Mapped[str | None]       = mapped_column(String, nullable=True)
    notes:          Mapped[str | None]       = mapped_column(Text, nullable=True)
    is_active:      Mapped[bool]             = mapped_column(Boolean, default=False)
    uploaded_by:    Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    uploaded_at:    Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class NotificationRead(Base):
    __tablename__ = "notification_reads"

    id:               Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    employee_id:      Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    notification_key: Mapped[str]       = mapped_column(String, nullable=False)
    read_at:          Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id:                Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:        Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    action:            Mapped[str]              = mapped_column(String, nullable=False)
    target_type:       Mapped[str | None]       = mapped_column(String, nullable=True)
    target_id:         Mapped[str | None]       = mapped_column(String, nullable=True)
    audit_metadata:    Mapped[dict | None]      = mapped_column("metadata", JSON, nullable=True)
    created_at:        Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ── Technology Utilization ────────────────────────────────────────────────────

class AppCategory(Base):
    __tablename__ = "app_categories"

    id:          Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:  Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("companies.id"))
    name:        Mapped[str]              = mapped_column(String, nullable=False)
    color:       Mapped[str]              = mapped_column(String, default="#6366f1")
    description: Mapped[str | None]       = mapped_column(Text, nullable=True)
    is_system:   Mapped[bool]             = mapped_column(Boolean, default=False)
    is_deleted:  Mapped[bool]             = mapped_column(Boolean, default=False)
    created_at:  Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    rules: Mapped[list["AppCategoryRule"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class AppCategoryRule(Base):
    __tablename__ = "app_category_rules"

    id:          Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("app_categories.id", ondelete="CASCADE"))
    company_id:  Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("companies.id"))
    app_pattern: Mapped[str]       = mapped_column(String, nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    category: Mapped["AppCategory"] = relationship(back_populates="rules")


# ── URL Blocking Policy ───────────────────────────────────────────────────────

class BlockedUrl(Base):
    __tablename__ = "blocked_urls"

    id:          Mapped[uuid.UUID]        = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    company_id:  Mapped[uuid.UUID]        = mapped_column(UUID, ForeignKey("companies.id"))
    pattern:     Mapped[str]              = mapped_column(String, nullable=False)
    # 'all' = block for every device in company, 'device' = specific device
    target_type: Mapped[str]              = mapped_column(String, default="all")
    device_id:   Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("devices.id"), nullable=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    notes:       Mapped[str | None]       = mapped_column(Text, nullable=True)
    is_active:   Mapped[bool]             = mapped_column(Boolean, default=True)
    is_deleted:  Mapped[bool]             = mapped_column(Boolean, default=False)
    created_by:  Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("employees.id"), nullable=True)
    created_at:  Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
