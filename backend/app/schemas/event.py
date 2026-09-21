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


# ── GET /v1/logs ──────────────────────────────────────────────────────────────

class LogEventOut(BaseModel):
    id:             str
    event_type:     str
    app_name:       Optional[str] = None
    window_title:   Optional[str] = None
    url:            Optional[str] = None
    duration:       int
    timestamp:      Optional[str] = None
    timestamp_unix: int
    device_id:      str
    hostname:       str
    employee_id:    Optional[str] = None
    employee_name:  Optional[str] = None


class LogsPageOut(BaseModel):
    items:     List[LogEventOut]
    # No exact total: an unbounded COUNT(*) over activity_events (tens of
    # millions of rows) on every page load is a cost this endpoint refuses to
    # pay. `has_more` is derived from a cheap one-row over-fetch instead — see
    # get_logs()'s docstring for the full reasoning.
    has_more:  bool
    offset:    int
    limit:     int
