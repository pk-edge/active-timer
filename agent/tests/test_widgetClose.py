"""The widget must veto casual closes but NEVER block an OS restart/shutdown.

Regression for the "TimeTracker is preventing shutdown" failure mode: the
unconditional closeEvent veto that keeps the timer alive on Alt+F4/⌘W was also
vetoing the close requests Windows and macOS send while shutting down.

Runs with the offscreen Qt platform — no display needed.
"""
import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pyside = pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent          # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


class _StubDB:
    def end_session(self, *_a, **_k): ...
    def start_session(self): return "stub"
    def flush_daily_secs(self, *_a): ...
    def get_today_active_secs_any_device(self): return 0


def _make_widget():
    from widget.floating_widget import FloatingWidget

    app = QApplication.instance() or QApplication([])
    shared = {
        "db": _StubDB(),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "sync_status": "ok",
        "today_active_secs": 0,
    }
    return app, FloatingWidget(shared)


def test_casual_close_is_vetoed_but_session_end_is_not():
    app, w = _make_widget()

    # Casual close (Alt+F4 / ⌘W): vetoed — the timer must stay alive.
    assert w.close() is False

    # OS shutdown/restart in progress: the veto must lift.
    w._allow_close = True
    assert w.close() is True


def test_macos_quit_event_lifts_the_veto():
    app, w = _make_widget()
    assert w._allow_close is False

    # macOS delivers shutdown/restart as a Quit event on the application —
    # the app-level filter must lift the veto BEFORE windows are closed.
    w.eventFilter(app, QEvent(QEvent.Type.Quit))
    assert w._allow_close is True
    assert w.close() is True


def test_commit_data_request_lifts_the_veto():
    app, w = _make_widget()
    w._allow_close = False

    # Windows emits commitDataRequest while the OS commits the session.
    w._on_session_end(None)
    assert w._allow_close is True
    assert w.close() is True


def test_power_actions_allowed_for_all_users(monkeypatch):
    """Restart/shutdown must work for admin AND non-admin users: plain yes/no
    confirmation, the OS command fires, and no elevation gate is involved
    (only Quit is admin-gated)."""
    import widget.floating_widget as fw

    app, w = _make_widget()
    issued = []
    monkeypatch.setattr(fw.subprocess, "Popen", lambda cmd, **k: issued.append(cmd))
    monkeypatch.setattr(fw.QMessageBox, "question",
                        staticmethod(lambda *a, **k: fw.QMessageBox.Yes))
    monkeypatch.setattr(fw.QApplication, "quit", staticmethod(lambda: None))

    w._shutdown_computer()
    w._restart_computer()

    assert len(issued) == 2, "both power commands must be issued without elevation"
    flat = " ".join(" ".join(c) for c in issued)
    if sys.platform == "win32":
        assert "/s" in flat and "/r" in flat
    elif sys.platform == "darwin":
        assert "shut down" in flat and "restart" in flat
    # The agent steps aside so it can never block the action it requested.
    assert w._allow_close is True
