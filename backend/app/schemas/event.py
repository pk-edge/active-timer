from typing import Optional, List, Union
from pydantic import BaseModel, field_validator

ALLOWED_EVENT_TYPES = {
    "window_change", "idle_start", "idle_end",
    "lock", "unlock", "url_visit",
}


class EventIn(BaseModel):
    id:           str
    event_type:   str
    app_name:     Optional[str] = None
    window_title: Optional[str] = None
    url:          Optional[str] = None
    duration:     int = 0
    timestamp:    Union[int, str]

    @field_validator("event_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"Unknown event_type: {v}")
        return v


class IngestRequest(BaseModel):
    events: List[EventIn]


class IngestResponse(BaseModel):
    received: int
    skipped:  int
