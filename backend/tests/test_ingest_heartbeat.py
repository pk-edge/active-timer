"""Presence heartbeat — an empty POST /v1/ingest keeps an idle/locked agent Online.

PRD-3738: devices.last_seen was only refreshed by real activity events, so an
idle or screen-locked (but healthy) Mac went Offline after the cutoff. The agent
now sends an empty ingest POST as a heartbeat; these cover the server side
(pure, no DB): cache semantics, flush draining and failure re-arming.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.ingest_queue import IngestQueue


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_touch_stamps_server_time_and_keeps_passive_state():
    q = IngestQueue()
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    q.device_cache["d1"] = {"last_seen": old, "latest_type": "idle_start",
                            "current_app": None, "updated_at": 1.0}
    q.touch("d1")
    st = q.device_cache["d1"]
    assert st["last_seen"] > old + timedelta(minutes=19)      # presence refreshed
    assert st["latest_type"] == "idle_start"                  # still passive, not "active"
    assert "d1" in q._touched


def test_touch_unknown_device_creates_no_cache_state():
    # No state to show yet: an invented entry (latest_type=None) would read as
    # "active". Presence still reaches every reader via the DB last_seen flush.
    q = IngestQueue()
    q.touch("new")
    assert "new" not in q.device_cache
    assert "new" in q._touched


def test_touch_ignores_a_skewed_future_event_clock():
    q = IngestQueue()
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    q.device_cache["d1"] = {"last_seen": future, "latest_type": "window_change"}
    q.touch("d1")
    assert q.device_cache["d1"]["last_seen"] == future        # never moves backwards


def test_touch_never_pushes_stateless_presence_to_sibling_workers():
    q = IngestQueue()
    seen = []
    q.on_state_update = lambda did, st: seen.append(did)
    q.device_cache["d1"] = {"last_seen": datetime.now(timezone.utc) - timedelta(minutes=5),
                            "latest_type": "lock"}
    q.touch("d1")
    assert seen == []


def test_real_event_after_heartbeat_is_not_dropped_by_server_time_stamp():
    # Heartbeat stamps server time; the user's next event carries a device time a
    # moment earlier (latency / small clock skew). It must still update state.
    q = IngestQueue()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    q._update_cache({"device_id": "d1", "row": {"timestamp": t0, "event_type": "idle_start"}})
    q.touch("d1")
    ev_ts = q.device_cache["d1"]["last_seen"] - timedelta(seconds=2)
    q._update_cache({"device_id": "d1", "row": {"timestamp": ev_ts, "event_type": "idle_end"}})
    st = q.device_cache["d1"]
    assert st["latest_type"] == "idle_end"
    assert st["last_seen"] >= ev_ts                            # presence never regresses


def test_flush_with_only_heartbeats_runs_presence_update():
    q = IngestQueue()
    calls = []

    published = []

    async def fake_touch(ids):
        calls.append(set(ids))
        return True
    q._touch_to_db = fake_touch

    async def fake_publish(evt):
        published.append(evt)
    import app.core.broadcaster as b
    orig, b.publish = b.publish, fake_publish
    try:
        q.touch("a")
        q.touch("b")
        _run(q._flush_now())
    finally:
        b.publish = orig
    assert calls == [{"a", "b"}]
    assert q._touched == set()                                # drained
    assert published == ["devices_updated"]                   # open dashboards refresh


def test_heartbeat_devices_are_rearmed_when_the_event_flush_fails():
    q = IngestQueue()

    async def failing_copy(rows, device_ids):
        raise OSError("db blip")

    async def noop_failure(batch, exc):
        return None
    q._copy_to_db = failing_copy
    q._handle_write_failure = noop_failure
    q.touch("idle-dev")
    q._q.put_nowait({"device_id": "busy-dev", "row": {"id": "e1"}})
    _run(q._flush_now())
    assert "idle-dev" in q._touched


def test_flush_with_events_folds_heartbeat_devices_into_last_seen_update():
    q = IngestQueue()
    captured = {}

    async def fake_copy(rows, device_ids):
        captured["devices"] = set(device_ids)
    q._copy_to_db = fake_copy
    q.touch("idle-dev")
    q._q.put_nowait({"device_id": "busy-dev", "row": {"id": "e1"}})

    async def noop_publish(*_a, **_k):
        return None
    import app.core.broadcaster as b
    orig, b.publish = b.publish, noop_publish
    try:
        _run(q._flush_now())
    finally:
        b.publish = orig
    assert captured["devices"] == {"busy-dev", "idle-dev"}


def test_failed_presence_write_is_rearmed_not_lost():
    q = IngestQueue()
    import app.db.base as base

    class _BadEngine:
        def connect(self):
            raise OSError("db down")
    orig, base.ingest_engine = base.ingest_engine, _BadEngine()
    try:
        _run(q._touch_to_db({"a"}))
    finally:
        base.ingest_engine = orig
    assert q._touched == {"a"}


def test_online_cutoff_leaves_margin_over_heartbeat_plus_flush():
    # agent heartbeat ~20 s + 30 s flush lag must stay well inside the cutoff
    assert get_settings().ONLINE_CUTOFF_SECS >= 90
