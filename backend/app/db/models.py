import uuid
from datetime import datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text
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
    created_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    company: Mapped["Company"]        = relationship(back_populates="employees")
    devices: Mapped[list["Device"]]   = relationship(back_populates="employee")


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
