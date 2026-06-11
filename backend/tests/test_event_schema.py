"""Agent event-type compatibility.

A batch containing an event type the backend doesn't know must NEVER be
rejected wholesale (422) — the agent's sync worker retries the same oldest
batch forever, so one unknown event type permanently jams that device's sync.
Unknown types are skipped per-event at the ingest endpoint instead.
"""

from app.schemas.event import ALLOWED_EVENT_TYPES, EventIn, IngestRequest


def test_meeting_and_blocked_events_are_known():
    assert {"meeting_start", "meeting_end", "url_blocked"} <= ALLOWED_EVENT_TYPES


def test_unknown_event_type_does_not_fail_validation():
    # Forward-compat: a newer agent may emit types this backend predates.
    req = IngestRequest(events=[
        EventIn(id="1", event_type="window_change", timestamp=1750000000),
        EventIn(id="2", event_type="some_future_event", timestamp=1750000000),
    ])
    assert len(req.events) == 2


def test_meeting_event_parses():
    e = EventIn(
        id="3",
        event_type="meeting_start",
        app_name="zoom.us",
        window_title="Weekly sync",
        timestamp=1750000000,
    )
    assert e.event_type == "meeting_start"
