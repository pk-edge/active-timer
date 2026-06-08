"""
WindowWatcher — polls the active foreground window every N seconds.

Changes:
  - Writes window_change event with explicit Unix timestamp (ts).
  - Persists current_title in shared dict so InputMonitor can check media context.
  - Detects meeting apps (Zoom, Teams, etc.) and emits meeting_start / meeting_end events.
  - Works on Windows, macOS, and Linux.
"""

import sys
import time
import logging

from config import Config

log = logging.getLogger("window_watcher")

MEETING_APPS = frozenset({
    "zoom", "us.zoom.videomeetings", "zoom.us",
    "microsoft teams", "teams",
    "lync",            # Skype for Business
    "webex", "cisco webex meetings",
    "facetime",
    "loom",
    "discord",
    "skype",
})


def _get_active_window() -> tuple[str, str]:
    """Returns (app_name, window_title) for the current foreground window."""
    try:
        if sys.platform == "win32":
            import ctypes, ctypes.wintypes as wt
            hwnd   = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf    = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            pid   = wt.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                import psutil
                proc = psutil.Process(pid.value)
                app  = proc.name().replace(".exe", "")
            except Exception:
                app = "unknown"
            return app, title

        elif sys.platform == "darwin":
            from AppKit import NSWorkspace
            ws  = NSWorkspace.sharedWorkspace()
            app = ws.frontmostApplication().localizedName()
            try:
                from Quartz import (
                    CGWindowListCopyWindowInfo,
                    kCGWindowListOptionOnScreenOnly,
                    kCGNullWindowID,
                )
                wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
                for w in wins:
                    if w.get("kCGWindowOwnerName") == app and w.get("kCGWindowLayer") == 0:
                        return app, w.get("kCGWindowName", "")
            except Exception:
                pass
            return app, ""

        else:
            import subprocess
            wid   = subprocess.check_output(["xdotool", "getactivewindow"]).strip()
            title = subprocess.check_output(["xdotool", "getwindowname", wid]).decode().strip()
            pid   = subprocess.check_output(["xdotool", "getwindowpid", wid]).decode().strip()
            try:
                import psutil
                app = psutil.Process(int(pid)).name()
            except Exception:
                app = "unknown"
            return app, title

    except Exception as e:
        log.debug("get_active_window error: %s", e)
        return "unknown", ""


def _is_meeting_app(app_name: str) -> bool:
    a = app_name.lower()
    return any(m in a for m in MEETING_APPS)


class WindowWatcher:
    def __init__(self, shared: dict):
        self._shared       = shared
        self._db           = shared["db"]
        self._pause        = shared["pause_event"]
        self._stop         = shared["stop_event"]
        self._interval     = Config.WINDOW_POLL_SECS
        self._last_app     = ""
        self._last_title   = ""
        self._app_start    = time.time()
        self._in_meeting   = False
        self._meeting_start= 0.0

    def run(self):
        log.info("WindowWatcher started (poll=%ds)", self._interval)
        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(1)
                continue

            app, title = _get_active_window()
            now        = int(time.time())  # UTC Unix

            if app != self._last_app or title != self._last_title:
                duration = int(time.time() - self._app_start)

                # Emit the outgoing window_change for the *previous* app
                if self._last_app:
                    self._db.insert_event({
                        "type":     "window_change",
                        "app":      self._last_app,
                        "title":    self._last_title,
                        "duration": duration,
                        "ts":       now,
                    })
                    self._shared["sync_trigger"].set()

                # Meeting lifecycle tracking
                was_meeting = self._in_meeting
                is_meeting  = _is_meeting_app(app)

                if is_meeting and not was_meeting:
                    self._in_meeting    = True
                    self._meeting_start = time.time()
                    self._db.insert_event({
                        "type":  "meeting_start",
                        "app":   app,
                        "title": title,
                        "ts":    now,
                    })
                    log.info("Meeting started: %s — %s", app, title)

                elif not is_meeting and was_meeting:
                    self._in_meeting = False
                    meeting_dur      = int(time.time() - self._meeting_start)
                    self._db.insert_event({
                        "type":     "meeting_end",
                        "app":      self._last_app,
                        "title":    self._last_title,
                        "duration": meeting_dur,
                        "ts":       now,
                    })
                    log.info("Meeting ended: %s (%ds)", self._last_app, meeting_dur)

                self._last_app   = app
                self._last_title = title
                self._app_start  = time.time()

                # Keep shared state up to date for InputMonitor media checks
                self._shared["current_app"]   = app
                self._shared["current_title"] = title

            time.sleep(self._interval)

        log.info("WindowWatcher stopped")
