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
from db.local_db               import LocalDB
from capture.window_watcher    import WindowWatcher
from capture.screenshotter     import Screenshotter
from capture.input_monitor     import InputMonitor
from capture.url_watcher       import UrlWatcher
from sync.sync_worker          import SyncWorker
from lock.lock_detector        import LockDetector
from updater                   import UpdateWorker
from policy.policy_manager     import PolicyManager

pause_event = threading.Event()
stop_event  = threading.Event()


def _enroll_background(shared: dict) -> None:
    """Run enrollment in a daemon thread so the widget appears immediately."""
    log.info("Background enrollment starting…")
    db = shared["db"]

    # ── Pre-load local DB time immediately ───────────────────────────────────
    # Timer starts ticking from the correct value even while enrollment is in
    # progress or when the backend is unreachable / token is invalid.
    # Unix timestamp used throughout — date.today() boundary for day grouping.
    local_early = db.get_today_active_secs_any_device()
    shared["today_active_secs"] = local_early
    log.info("Timer pre-loaded from local DB: %ds", local_early)

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

    db.set_device_id(device_id)

    # ── Reconcile with server ────────────────────────────────────────────────
    # Rule: always use the LARGEST of (server, local).
    # • Server is authoritative for events already synced to this backend.
    # • Local accumulator covers the current session + events not yet uploaded.
    # • On re-enrollment (new device_id) the local accumulator may belong to
    #   the previous device_id, so scan ALL today entries regardless of id.
    local_secs  = db.get_today_active_secs_any_device()
    server_secs = _fetch_today_active(ident.get("jwt_token", ""))

    if server_secs is not None and server_secs > 0:
        best = max(server_secs, local_secs)
        log.info("Today active — server=%ds local=%ds → using %ds", server_secs, local_secs, best)
    else:
        best = local_secs
        reason = "server returned 0" if server_secs == 0 else "server unreachable"
        log.info("Today active — %s, using local: %ds", reason, best)

    # Persist authoritative value so restarts resume correctly (unix-keyed by date)
    db.flush_daily_secs_absolute(best)

    # Signal widget to apply server-corrected value if it differs from pre-load
    shared["today_active_secs"] = best

    session_id = db.start_session()
    shared["current_session_id"] = session_id
    shared["sync_status"] = "syncing"
    log.info("Session started: %s (today so far: %ds)", session_id, best)


def _fetch_today_active(jwt_token: str) -> int | None:
    """Fetch today's authoritative active-seconds from the backend (device JWT)."""
    try:
        import httpx
        from config import Config
        r = httpx.get(
            Config.DEVICE_TODAY_ENDPOINT,
            headers={"Authorization": f"Bearer {jwt_token}"},
            timeout=8,
        )
        if r.status_code == 200:
            return int(r.json().get("active_seconds_today", 0))
    except Exception as e:
        log.debug("Could not fetch today-active from server: %s", e)
    return None


def main() -> None:
    log.info("TimeTracker agent starting")

    # 1. Local DB — schema only, device_id set after enrollment
    db = LocalDB(Config.DB_PATH)
    db.init_schema()

    # 2. Shared state
    policy_manager = PolicyManager(db)
    shared = {
        "db":                  db,
        "pause_event":         pause_event,
        "stop_event":          stop_event,
        "sync_trigger":        threading.Event(),
        "current_app":         "",
        "current_title":       "",   # kept current by WindowWatcher for idle media detection
        "sync_status":         "enrolling",
        "current_session_id":  None,
        "new_session_event":   threading.Event(),
        "lock_time":           None,
        "policy_manager":      policy_manager,
        "today_active_secs":   None,  # None until enrollment resolves the real value
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
        from PySide6.QtCore    import QTimer
        from widget.floating_widget import FloatingWidget

        app = QApplication.instance() or QApplication(sys.argv)
        # Don't quit when the floating window is closed — it just hides.
        app.setQuitOnLastWindowClosed(False)

        widget = FloatingWidget(shared)
        widget.show()

        # ── Periodic daily flush — saves active seconds every 60s ────────────
        # Baseline is set to the pre-loaded daily total so only GENUINELY NEW
        # seconds are persisted.  Negative deltas (day rollover) are ignored.
        from PySide6.QtCore import QTimer

        # Baseline = today's already-persisted seconds read synchronously from DB.
        # Reading here (not from shared["today_active_secs"]) avoids a race with
        # the background enrollment thread.  Only NEW seconds are flushed.
        _last_flushed_secs = [shared["db"].get_today_active_secs_any_device()]

        def _flush_daily():
            current = widget.elapsed
            delta   = current - _last_flushed_secs[0]
            if delta > 0:           # positive only — ignore day-rollover negatives
                shared["db"].flush_daily_secs(delta)
            _last_flushed_secs[0] = current

        flush_timer = QTimer()
        flush_timer.setInterval(60_000)  # every 60 seconds
        flush_timer.timeout.connect(_flush_daily)
        flush_timer.start()

        def _on_quit():
            flush_timer.stop()
            _flush_daily()            # final flush on clean exit
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
