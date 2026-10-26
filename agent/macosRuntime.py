"""
macOS-only process-lifecycle hardening (PRD-3738). Never imported on Windows.

The agent is a Qt app whose telemetry lives in daemon worker threads. Three
failure modes left a Mac with a *live process that tracks nothing* — shown in the
dashboard as Offline, stale-idle, with an empty activity log:

  1. SIGTERM (installer ``pkill``), SIGINT and the updater only set
     ``stop_event``. Every worker stopped but nothing ever quit the Qt event
     loop, so the process lingered: launchd saw it alive (no relaunch) and
     ``open -a`` just re-activated it.                -> install_qt_shutdown_watcher
  2. A worker that raised died silently and was never restarted. -> run_supervised
  3. Nothing stopped a second copy (``open -a`` + launchd at login) from running
     alongside the first and fighting over queue.db / identity.json.
                                                      -> acquire_single_instance
Plus App Nap / automatic termination opt-out for a windowless agent
(begin_activity).
"""
import fcntl
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("macosRuntime")

LOCK_NAME = "agent.lock"
SHUTDOWN_POLL_MS = 250
# Hard deadline: if the Qt loop has not returned this long after stop_event was
# set, exit anyway — a wedged shutdown must never leave a zombie behind again.
SHUTDOWN_GRACE_SECS = 10.0

_lock_fh = None          # held for the process lifetime (flock dies with the process)
_activity_token = None   # NSProcessInfo activity token — must stay referenced


# ── Single instance ──────────────────────────────────────────────────────────

def acquire_single_instance(base_dir: Path) -> bool:
    """Take an exclusive, non-blocking flock on <base_dir>/agent.lock.

    Returns False when another agent already holds it (caller exits 0 — with
    launchd's KeepAlive{SuccessfulExit:false} that is not respawned). The kernel
    drops the lock when the process dies for any reason, so a crashed agent can
    never leave a stale lock behind.
    """
    global _lock_fh
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        fh = open(base_dir / LOCK_NAME, "a+")
    except OSError as e:
        log.warning("Single-instance lock unavailable (%s) — continuing without it", e)
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return False          # genuinely held by another running agent
    except OSError as e:
        # e.g. a network home folder without flock support: run unguarded rather
        # than exit and never track at all.
        log.warning("Single-instance lock unsupported here (%s) — continuing without it", e)
        fh.close()
        return True
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    _lock_fh = fh
    return True


# ── App Nap / automatic termination ──────────────────────────────────────────

def begin_activity(reason: str = "TimeTracker activity capture") -> bool:
    """Opt the process out of App Nap and automatic/sudden termination.

    A windowless background agent is exactly what macOS naps (coalesced timers,
    throttled threads) or terminates under memory pressure. System idle sleep is
    still allowed — the Mac may sleep normally.
    """
    global _activity_token
    try:
        from Foundation import NSProcessInfo
        try:
            from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep as opts
        except ImportError:
            opts = 0x00FFFFFF            # NSActivityUserInitiated minus IdleSystemSleepDisabled
        pi = NSProcessInfo.processInfo()
        _activity_token = pi.beginActivityWithOptions_reason_(opts, reason)
        pi.disableAutomaticTermination_(reason)
        pi.disableSuddenTermination()
        return True
    except Exception as e:  # noqa: BLE001 — a hardening nicety, never fatal
        log.debug("begin_activity unavailable: %s", e)
        return False


# ── Worker supervision ───────────────────────────────────────────────────────

def run_supervised(
    factory: Callable[[], object],
    stop_event: threading.Event,
    name: str,
    backoff: float = 5.0,
    max_backoff: float = 60.0,
    sleep: Callable[[float], bool] = None,
) -> None:
    """Run ``factory().run()``; if it raises or returns while the agent is still
    meant to be running, log it loudly and start a FRESH instance after a short
    backoff (a new instance, because workers close their HTTP clients in run()'s
    ``finally``). Returns only once ``stop_event`` is set."""
    wait = sleep or stop_event.wait
    delay = backoff
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            factory().run()
            if stop_event.is_set():
                return
            log.error("%s exited unexpectedly — restarting in %.0fs", name, delay)
        except Exception:  # noqa: BLE001
            log.exception("%s crashed — restarting in %.0fs", name, delay)
        if wait(delay):
            return
        # A worker that stayed up for a while was healthy: reset the backoff.
        delay = backoff if time.monotonic() - started > 5 * max_backoff else min(delay * 2, max_backoff)


# ── Qt shutdown ──────────────────────────────────────────────────────────────

def install_qt_shutdown_watcher(app, stop_event: threading.Event, grace: Optional[float] = None) -> object:
    """Quit the Qt loop when ``stop_event`` is set (SIGTERM/SIGINT/update).

    Python signal handlers only run when the interpreter gets control, which a
    Qt-owned main thread gives it on timer ticks — so a Qt-thread QTimer is both
    the place that notices ``stop_event`` and the place that calls ``app.quit()``.
    After the quit request, a daemon timer hard-exits the process if the loop
    still has not returned. Returns the QTimer (caller keeps it referenced).
    """
    from PySide6.QtCore import QTimer

    grace = SHUTDOWN_GRACE_SECS if grace is None else grace
    state = {"armed": False}

    def _arm_deadline():
        if state["armed"]:
            return
        state["armed"] = True
        killer = threading.Timer(grace, _hard_exit)
        killer.daemon = True
        killer.start()

    def _tick():
        if state["armed"] or not stop_event.is_set():
            return
        log.info("Stop requested — quitting Qt event loop")
        _arm_deadline()
        app.quit()

    # Tray quit / power-off / restart call QApplication.quit() directly: arm the
    # same deadline there, so a hung final flush can never block the shutdown.
    app.aboutToQuit.connect(_arm_deadline)

    timer = QTimer()
    timer.setInterval(SHUTDOWN_POLL_MS)
    timer.timeout.connect(_tick)
    timer.start()
    return timer


def _hard_exit() -> None:
    log.error("Qt event loop did not exit within the shutdown grace period — forcing exit")
    logging.shutdown()
    os._exit(0)
