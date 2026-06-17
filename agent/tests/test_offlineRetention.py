"""Offline-first storage + 3-day retention.

Rules under test:
  - Every record lands in local queue.db and SURVIVES agent restarts until the
    backend confirms it (offline devices sync whenever connectivity returns).
  - Confirmed (synced/uploaded) records are kept LOCAL_RETENTION_DAYS, then
    flushed. Unsynced records are NEVER deleted — they wait for sync.
  - After reconnecting, a full batch re-arms the sync trigger so a backlog
    drains continuously instead of one batch per 5-second interval.
"""
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config          # noqa: E402
from db.local_db import LocalDB    # noqa: E402

OLD_TS = int(time.time()) - 4 * 86400      # older than the 3-day window
NEW_TS = int(time.time()) - 60


def _db(tmp_path) -> LocalDB:
    db = LocalDB(tmp_path / "queue.db")
    db.init_schema()
    db.set_device_id("dev-1")
    return db


def _insert(db, ts, synced=False):
    rid = db.insert_event({"type": "window_change", "app": "Code", "ts": ts})
    if synced:
        db.mark_events_synced([rid])
    return rid


# ── Offline queue survives restarts and syncs later ───────────────────────────

def test_offline_events_survive_restart_and_sync_later(tmp_path):
    db = _db(tmp_path)
    ids = [_insert(db, NEW_TS - i) for i in range(5)]
    assert db.unsynced_count() == 5

    # Agent restart while still offline — queue intact.
    db2 = LocalDB(tmp_path / "queue.db")
    db2.init_schema()
    db2.set_device_id("dev-1")
    pending = db2.get_unsynced_events(limit=100)
    assert {e["id"] for e in pending} == set(ids)

    # Connectivity returns → server confirms → marked synced.
    db2.mark_events_synced([e["id"] for e in pending])
    assert db2.unsynced_count() == 0


def test_first_run_offline_buffers_until_enrollment(tmp_path):
    """A NEVER-enrolled device with no connectivity must still queue events;
    enrollment later adopts them and they sync like any other rows."""
    db = LocalDB(tmp_path / "queue.db")
    db.init_schema()
    assert db.device_id == ""

    ids = {db.insert_event({"type": "window_change", "app": "Code", "ts": NEW_TS - i})
           for i in range(3)}
    assert "" not in ids, "events must be buffered, not dropped"
    assert db.unsynced_count() == 3

    # Enrollment finally succeeds → buffered rows adopt the real device id.
    db.set_device_id("dev-real")
    pending = db.get_unsynced_events(limit=10)
    assert {e["id"] for e in pending} == ids
    assert all(e["device_id"] == "dev-real" for e in pending)


# ── Retention: synced >3d flushed, unsynced wait forever ──────────────────────

def test_retention_flushes_only_old_synced_events(tmp_path):
    db = _db(tmp_path)
    old_synced   = _insert(db, OLD_TS, synced=True)
    old_unsynced = _insert(db, OLD_TS)            # still waiting for sync
    new_synced   = _insert(db, NEW_TS, synced=True)

    removed = db.purge_synced_records(days=Config.LOCAL_RETENTION_DAYS)
    assert removed["events"] == 1

    with db._conn() as conn:
        kept = {r["id"] for r in conn.execute("SELECT id FROM events").fetchall()}
    assert old_synced not in kept, "synced record past retention must be flushed"
    assert old_unsynced in kept,   "UNSYNCED record must wait for sync, never deleted"
    assert new_synced in kept,     "synced record inside the window must be kept"


def test_retention_screenshots_and_daily_totals(tmp_path):
    db = _db(tmp_path)

    png = tmp_path / "old.png"
    png.write_bytes(b"fake")
    uploaded_id = db.insert_screenshot(str(png), 4)
    pending_id  = db.insert_screenshot(str(tmp_path / "pending.png"), 4)
    with db._lock, db._conn() as conn:   # age both beyond the window
        conn.execute("UPDATE screenshots SET captured_at=?", (OLD_TS,))
    db.mark_screenshot_uploaded(uploaded_id, "s3/key.png")

    db.flush_daily_secs(120)             # today's accumulator row
    with db._lock, db._conn() as conn:   # plus a stale one from 5 days ago
        conn.execute(
            "INSERT INTO daily_totals (date, device_id, active_secs, idle_secs, updated_at) "
            "VALUES ('2020-01-01', 'dev-1', 999, 0, 0)"
        )

    removed = db.purge_synced_records(days=Config.LOCAL_RETENTION_DAYS)
    assert removed["screenshots"] == 1
    assert removed["daily_totals"] == 1
    assert not png.exists(), "uploaded screenshot file must be removed from disk"

    with db._conn() as conn:
        kept = {r["id"] for r in conn.execute("SELECT id FROM screenshots").fetchall()}
    assert pending_id in kept, "un-uploaded screenshot must wait for sync"
    assert uploaded_id not in kept
    assert db.get_today_active_secs_any_device() == 120, "today's accumulator untouched"


# ── Reconnect drain: full batch re-arms the trigger ───────────────────────────

class _OkResponse:
    status_code = 200
    def raise_for_status(self): ...


def test_backlog_drains_continuously_after_reconnect(tmp_path, monkeypatch):
    import sync.sync_worker as sw

    db = _db(tmp_path)
    for i in range(Config.SYNC_BATCH_SIZE + 10):
        _insert(db, NEW_TS - i)

    shared = {
        "db": db,
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "sync_trigger": threading.Event(),
        "sync_status": "offline",
    }
    worker = sw.SyncWorker(shared)
    monkeypatch.setattr(sw.identity, "load", lambda: {"jwt_token": "tok"})
    monkeypatch.setattr(worker._http, "post", lambda *a, **k: _OkResponse())

    # First batch after reconnect: full → trigger re-armed for immediate next run.
    worker._sync_events()
    assert db.unsynced_count() == 10
    assert shared["sync_trigger"].is_set(), "full batch must re-arm the trigger"

    shared["sync_trigger"].clear()
    worker._sync_events()
    assert db.unsynced_count() == 0
    assert not shared["sync_trigger"].is_set(), "partial batch must not re-arm"
    assert shared["sync_status"] == "ok"

    # Hourly retention pass runs from the worker loop without error.
    worker._last_purge = 0.0
    worker._maybe_purge()
    assert worker._last_purge > 0
