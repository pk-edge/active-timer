import logging
import signal
import sys
import threading
import time

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(Config.LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")

import enrollment
import autostart
from db.local_db            import LocalDB
from capture.window_watcher import WindowWatcher
from capture.screenshotter  import Screenshotter
from capture.input_monitor  import InputMonitor
from capture.url_watcher    import UrlWatcher
from sync.sync_worker       import SyncWorker
from lock.lock_detector     import LockDetector
from updater                import UpdateWorker

pause_event = threading.Event()
stop_event  = threading.Event()


def _enroll_background(shared: dict) -> None:
    """Run enrollment in a daemon thread so the widget appears immediately."""
    log.info("Background enrollment starting…")
    try:
        ident = enrollment.ensure_enrolled(shared=shared)
    except RuntimeError as e:
        # Invalid token / missing config — retrying will not help until fixed.
        log.error("Enrollment failed: %s", e)
        shared["sync_status"] = "error"
        return
    except Exception as e:
        log.error("Enrollment failed: %s — will retry on next launch", e)
        shared["sync_status"] = "error"
        return

    device_id = ident["device_id"]
    log.info("Enrolled: device_id=%s", device_id)

    db = shared["db"]
    db.set_device_id(device_id)

    # Load today's already-accumulated active seconds BEFORE starting the new session
    # so the widget can resume the correct count.
    shared["today_active_secs"] = db.get_today_active_secs()

    session_id = db.start_session()
    shared["current_session_id"] = session_id
    shared["sync_status"] = "syncing"
    log.info("Session started: %s (today so far: %ds)", session_id, shared["today_active_secs"])


def main() -> None:
    log.info("TimeTracker agent starting")

    # 1. Local DB — schema only, device_id set after enrollment
    db = LocalDB(Config.DB_PATH)
    db.init_schema()

    # 2. Shared state
    shared = {
        "db":                  db,
        "pause_event":         pause_event,
        "stop_event":          stop_event,
        "sync_trigger":        threading.Event(),  # set by capture threads → wakes SyncWorker immediately
        "current_app":         "",
        "sync_status":         "enrolling",
        "current_session_id":  None,
        "new_session_event":   threading.Event(),
        "lock_time":           None,
    }

    # 3. Register auto-start on login
    autostart.install()

    # 4. Daemon worker threads (silently skip DB writes until enrolled)
    workers = [
        WindowWatcher(shared),
        # Screenshotter(shared),  # disabled — re-enable when needed
        InputMonitor(shared),
        UrlWatcher(shared),
        SyncWorker(shared),
        UpdateWorker(shared),
    ]
    for w in workers:
        t = threading.Thread(target=w.run, name=w.__class__.__name__, daemon=True)
        t.start()

    # 5. Background enrollment — widget shows before this finishes
    threading.Thread(target=_enroll_background, args=(shared,), daemon=True).start()

    # 6. Lock detector
    lock_detector = LockDetector(
        on_lock=_make_lock_handler(shared),
        on_unlock=_make_unlock_handler(shared),
    )
    lock_detector.start()

    # 7. SIGINT / SIGTERM → clean shutdown
    def _shutdown(sig, _frame):
        log.info("Shutdown signal %s received", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    # 8. Qt widget — BLOCKS until user quits via tray menu or SIGTERM
    _run_widget(shared)

    # If Qt unavailable keep main alive
    while not stop_event.is_set():
        time.sleep(1)


def _run_widget(shared: dict) -> None:
    """
    Create and run the PySide6 floating timer.

    Qt must own the main thread on macOS; this call is BLOCKING.
    Returns only when QApplication.quit() is called (tray "Quit" or stop_event).
    """
    try:
        from PySide6.QtWidgets import QApplication
        from widget.floating_widget import FloatingWidget

        app = QApplication.instance() or QApplication(sys.argv)
        # Don't quit when the floating window is closed — it just hides.
        app.setQuitOnLastWindowClosed(False)

        widget = FloatingWidget(shared)
        widget.show()

        def _on_quit():
            stop_event.set()
            shared["db"].end_session(
                shared.get("current_session_id"), widget.elapsed
            )

        app.aboutToQuit.connect(_on_quit)

        exit_code = app.exec()   # blocks here
        log.info("Qt event loop exited (code=%d)", exit_code)
        stop_event.set()

    except ImportError:
        log.warning("PySide6 not available — running headless (no timer widget)")


def _make_lock_handler(shared: dict):
    def _handler():
        log.info("Screen locked — pausing capture")
        shared["lock_time"] = time.time()
        pause_event.set()
        shared["db"].insert_event({"type": "lock", "duration": 0})
        shared["sync_trigger"].set()
    return _handler


def _make_unlock_handler(shared: dict):
    def _handler():
        lock_time = shared.get("lock_time")
        shared["lock_time"] = None
        locked_secs = (time.time() - lock_time) if lock_time else 0
        log.info("Screen unlocked — locked for %.0f s", locked_secs)
        if locked_secs >= 3600:
            log.info("Lock > 1 hour — signalling new attendance session")
            shared["new_session_event"].set()
        pause_event.clear()
        shared["db"].insert_event({"type": "unlock", "duration": int(locked_secs)})
        shared["sync_trigger"].set()
    return _handler


def _watchdog_entry() -> None:
    from protection.win_guard import watchdog_tick
    watchdog_tick()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--watchdog":
        _watchdog_entry()
        sys.exit(0)

    if sys.platform == "win32":
        from protection.win_guard import acquire_single_instance, protect_process, install_watchdog
        if not acquire_single_instance():
            sys.exit(0)
        protect_process()
        install_watchdog()

    main()
