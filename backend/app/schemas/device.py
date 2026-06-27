from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DeviceOut(BaseModel):
    id:             str
    hostname:       str
    platform:       str
    employee_id:    Optional[str] = None
    employee_name:  Optional[str]
    last_seen:      Optional[datetime]
    last_seen_unix: Optional[int]
    is_online:      bool
    is_archived:    bool = False
    is_deleted:     bool = False
    # Agent visibility: raw per-device override (None = inherit company default)
    # and the effective value the agent actually applies.
    headless_override: Optional[bool] = None
    headless:          bool = False

    model_config = {"from_attributes": True}


class AssignDeviceRequest(BaseModel):
    employee_id: Optional[str] = None   # null/omitted = unassign


class HeadlessRequest(BaseModel):
    # null = inherit the company default; true/false = force this device.
    override: Optional[bool] = None


class TimelineEventOut(BaseModel):
    id:             str
    event_type:     str
    app_name:       Optional[str]
    window_title:   Optional[str]
    url:            Optional[str]
    duration:       int
    timestamp:      datetime
    timestamp_unix: int


class DailyStatOut(BaseModel):
    date:           str
    active_seconds: int
    idle_seconds:   int
    total_seconds:  int
