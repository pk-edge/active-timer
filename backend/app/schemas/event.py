from typing import Optional, List, Union
from pydantic import BaseModel

# Known event types. Events outside this set are SKIPPED per-event at the
# ingest endpoint (counted in `skipped`) — never a 422 for the whole batch.
# A newer agent emitting a new event type must not jam sync on an older
# backend: the sync worker retries the same oldest batch forever on 4xx.
ALLOWED_EVENT_TYPES = {
    "window_change", "idle_start", "idle_end",
    "lock", "unlock", "url_visit", "url_blocked",
    "meeting_start", "meeting_end",
}


class EventIn(BaseModel):
    id:           str
    event_type:   str
    app_name:     Optional[str] = None
    window_title: Optional[str] = None
    url:          Optional[str] = None
    duration:     int = 0
    timestamp:    Union[int, str]


class IngestRequest(BaseModel):
    events: List[EventIn]


class IngestResponse(BaseModel):
    received: int
    skipped:  int
