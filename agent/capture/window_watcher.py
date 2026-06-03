"""
WindowWatcher — polls the active foreground window every N seconds.
Writes a window_change event whenever the app or title changes.
Respects the global pause_event (fired on lock).
"""

import sys
import time
import logging

log = logging.getLogger("window_watcher")


def _get_active_window() -> tuple[str, str]:
    """Returns (app_name, window_title) for the current foreground window."""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as wt
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            # Get process name
            pid = wt.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            import psutil
            try:
                proc = psutil.Process(pid.value)
                app  = proc.name().replace(".exe", "")
            except Exception:
                app = "unknown"
            return app, title

        elif sys.platform == "darwin":
            from AppKit import NSWorkspace
            ws  = NSWorkspace.sharedWorkspace()
            app = ws.frontmostApplication().localizedName()
            # Active window title via Quartz (best effort)
            try:
                from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
                for w in wins:
                    if w.get("kCGWindowOwnerName") == app and w.get("kCGWindowLayer") == 0:
                        return app, w.get("kCGWindowName", "")
            except Exception:
                pass
            return app, ""

        else:
            # Linux: use xdotool
            import subprocess
            wid   = subprocess.check_output(["xdotool", "getactivewindow"]).strip()
            title = subprocess.check_output(["xdotool", "getwindowname", wid]).decode().strip()
            pid   = subprocess.check_output(["xdotool", "getwindowpid", wid]).decode().strip()
            import psutil
            try:
                app = psutil.Process(int(pid)).name()
            except Exception:
                app = "unknown"
            return app, title

    except Exception as e:
        log.debug("get_active_window error: %s", e)
        return "unknown", ""


class WindowWatcher:
    def __init__(self, shared: dict):
        self._shared     = shared
        self._db         = shared["db"]
        self._pause      = shared["pause_event"]
        self._stop       = shared["stop_event"]
        self._interval   = 5  # seconds
        self._last_app   = ""
        self._last_title = ""
        self._app_start  = time.time()

    def run(self):
        log.info("WindowWatcher started")
        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(1)
                continue

            app, title = _get_active_window()

            if app != self._last_app or title != self._last_title:
                duration = int(time.time() - self._app_start)
                if self._last_app:
                    self._db.insert_event({
                        "type":     "window_change",
                        "app":      self._last_app,
                        "title":    self._last_title,
                        "duration": duration,
                    })
                    self._shared["sync_trigger"].set()

                self._last_app   = app
                self._last_title = title
                self._app_start  = time.time()
                self._shared["current_app"] = app

            time.sleep(self._interval)
        log.info("WindowWatcher stopped")
