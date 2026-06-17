"""Same-day timer resume: after any agent/machine restart the ACTIVE TODAY
timer must continue from the persisted value — never reset, never jump ahead.

Covers the three failure modes that broke this:
  1. unenrolled flushes were dropped → offline boot lost the day's progress
  2. server-reconciled absolute write + in-memory flush baseline → double count
  3. flush firing across midnight wrote yesterday's total into today's row
     (guarded in main._flush_daily via the persisted-delta computation)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.local_db import LocalDB  # noqa: E402


def _fresh_db(tmp_path, name="q.db"):
    db = LocalDB(tmp_path / name)
    db.init_schema()
    return db


def test_timer_resumes_after_restart_same_day(tmp_path):
    db = _fresh_db(tmp_path)
    db.set_device_id("dev-1")
    db.flush_daily_secs(120)
    db.flush_daily_secs(60)
    assert db.get_today_active_secs_any_device() == 180

    # Agent restart: a brand-new LocalDB over the same file must resume at 180.
    db2 = LocalDB(tmp_path / "q.db")
    db2.init_schema()
    assert db2.get_today_active_secs_any_device() == 180


def test_unenrolled_flush_still_persists(tmp_path):
    """Backend down at boot → no device id — the day's time must still survive."""
    db = _fresh_db(tmp_path)
    assert db.device_id == ""
    db.flush_daily_secs(300)
    assert db.get_today_active_secs_any_device() == 300

    # Later enrollment keeps accumulating into the same day.
    db.set_device_id("dev-1")
    db.flush_daily_secs(60)
    assert db.get_today_active_secs_any_device() == 360


def test_server_correction_never_double_counts(tmp_path):
    """The flush delta is computed against the PERSISTED value (main._flush_daily),
    so the absolute server write cannot be added twice."""
    db = _fresh_db(tmp_path)
    db.set_device_id("dev-1")

    db.flush_daily_secs(100)                 # local progress before enrollment sync
    db.flush_daily_secs_absolute(500)        # server knows more → authoritative
    assert db.get_today_active_secs_any_device() == 500

    # Widget jumped to 500 and ticked one minute → elapsed=560.
    # main._flush_daily: delta = elapsed - persisted.
    elapsed = 560
    delta = elapsed - db.get_today_active_secs_any_device()
    db.flush_daily_secs(delta)
    assert db.get_today_active_secs_any_device() == 560   # NOT 660

    # Convergence: repeating the flush with no new ticks adds nothing.
    delta = elapsed - db.get_today_active_secs_any_device()
    db.flush_daily_secs(delta)
    assert db.get_today_active_secs_any_device() == 560


def test_negative_and_zero_deltas_ignored(tmp_path):
    db = _fresh_db(tmp_path)
    db.set_device_id("dev-1")
    db.flush_daily_secs(50)
    db.flush_daily_secs(0)
    db.flush_daily_secs(-999)   # day-rollover artifact — must not corrupt the row
    assert db.get_today_active_secs_any_device() == 50
