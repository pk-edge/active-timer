from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DeviceOut(BaseModel):
    id:             str
    hostname:       str
    platform:       str
    employee_name:  Optional[str]
    last_seen:      Optional[datetime]
    last_seen_unix: Optional[int]
    is_online:      bool

    model_config = {"from_attributes": True}


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
