"""Tests for the URL-fetching / meeting-detection / idle-exemption fixes."""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.local_db import LocalDB
from capture.url_watcher import _copy_locked_db, _remove_db_copy, _classify_url
from capture.window_watcher import _is_meeting_context, _is_media_context
from capture.input_monitor import _is_media_or_meeting


# ── LocalDB.insert_event timestamp ────────────────────────────────────────────

def _make_db(tmp_path: Path) -> LocalDB:
    db = LocalDB(tmp_path / "queue.db")
    db.init_schema()
    db.set_device_id("test-device")
    return db


def test_insert_event_honors_capture_ts(tmp_path):
    db = _make_db(tmp_path)
    visit_ts = int(time.time()) - 3600  # a visit from an hour ago
    db.insert_event({"type": "url_visit", "url": "https://example.com", "ts": visit_ts})
    row = db.get_unsynced_events(1)[0]
    assert row["ts"] == visit_ts


def test_insert_event_defaults_to_now(tmp_path):
    db = _make_db(tmp_path)
    before = int(time.time())
    db.insert_event({"type": "idle_end"})
    row = db.get_unsynced_events(1)[0]
    assert before <= row["ts"] <= int(time.time())


# ── WAL sidecar copy ──────────────────────────────────────────────────────────

def test_copy_locked_db_includes_wal_sidecar(tmp_path):
    src = tmp_path / "History.db"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    # Leave the connection open so the row stays in the -wal sidecar
    assert (tmp_path / "History.db-wal").exists()

    tmp = _copy_locked_db(src)
    assert tmp is not None
    assert Path(tmp + "-wal").exists()
    copy = sqlite3.connect(tmp)
    assert copy.execute("SELECT x FROM t").fetchone()[0] == 42
    copy.close()
    conn.close()
    _remove_db_copy(tmp)
    assert not Path(tmp).exists()
    assert not Path(tmp + "-wal").exists()


# ── Meeting / media context classification ────────────────────────────────────

@pytest.mark.parametrize("app,title,expected", [
    ("zoom.us", "Zoom Meeting", True),                          # native app
    ("Microsoft Teams", "Weekly sync", True),                   # native app
    ("Google Chrome", "Meet – abc-defg-hij", True),             # Meet tab
    ("Safari", "Zoom Meeting - Zoom", True),                    # Zoom web
    ("Microsoft Edge", "Standup | Microsoft Teams", True),      # Teams web
    ("Google Chrome", "Stack Overflow - python", False),
    ("Code", "main.py — active-time", False),
])
def test_meeting_context(app, title, expected):
    assert _is_meeting_context(app, title) is expected


@pytest.mark.parametrize("app,title,expected", [
    ("Google Chrome", "Conference talk - YouTube", True),
    ("Firefox", "Stranger Things | Netflix", True),
    ("Spotify", "", True),                                      # native media app
    ("Google Chrome", "GitHub - pull request", False),
])
def test_media_context(app, title, expected):
    assert _is_media_context(app, title) is expected


# ── Idle exemption via shared state ───────────────────────────────────────────

def test_idle_exemption_uses_shared_meeting_flag():
    shared = {"in_meeting": True, "media_active": False,
              "current_app": "", "current_title": ""}
    assert _is_media_or_meeting(shared) is True


def test_idle_exemption_uses_shared_media_flag():
    shared = {"in_meeting": False, "media_active": True,
              "current_app": "", "current_title": ""}
    assert _is_media_or_meeting(shared) is True


def test_idle_exemption_falls_back_to_title_match():
    shared = {"in_meeting": False, "media_active": False,
              "current_app": "Google Chrome",
              "current_title": "Lo-fi beats - YouTube"}
    assert _is_media_or_meeting(shared) is True


def test_no_exemption_for_regular_work():
    shared = {"in_meeting": False, "media_active": False,
              "current_app": "Code", "current_title": "main.py"}
    assert _is_media_or_meeting(shared) is False


# ── URL classification still works ────────────────────────────────────────────

def test_url_classification():
    assert _classify_url("https://meet.google.com/abc-defg-hij") == "meeting"
    assert _classify_url("https://www.youtube.com/watch?v=x") == "media"
    assert _classify_url("https://github.com/org/repo") == "productivity"
