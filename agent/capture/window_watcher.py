"""
WindowWatcher — polls the active foreground window every N seconds.

Responsibilities:
  - Emits window_change slices with delta durations: on every window switch
    AND every ACTIVITY_HEARTBEAT_SECS while the user stays in one window, so
    the backend receives a continuous activity log and SUM(duration) stays
    exact. Slices are suppressed while the user is idle.
  - Detects meeting context (native apps AND browser tabs — Google Meet,
    Zoom web, Teams web…) and emits meeting_start / meeting_end events.
  - Publishes shared["current_app"] / ["current_title"] / ["in_meeting"] /
    ["media_active"] for InputMonitor's idle exemptions.
  - macOS: falls back to AppleScript for the active browser-tab title when
    Quartz window titles are unavailable (no Screen Recording permission).
  - Works on Windows, macOS, and Linux. The poll loop survives any error.
"""

import subprocess
import sys
import time
import logging

from config import Config

log = logging.getLogger("window_watcher")

# Apps that represent the OS lock/login screen — never count as active work.
# These appear as the foreground window while the screen is locked.
LOCK_SCREEN_APPS = frozenset({
    "loginwindow",          # macOS lock screen
    "screensaverengine",    # macOS screen saver process
    "lockapp",              # Windows 10/11 lock screen
    "logonui",              # Windows login/credential UI
    "system idle process",  # Windows idle placeholder
    "xscreensaver",         # Linux X11 screen saver
    "gnome-screensaver",    # Linux GNOME screen saver
    "xfce4-screensaver",    # Linux XFCE screen saver
})

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

# Keep a meeting "open" through brief context switches (checking notes,
# replying on Slack mid-call) instead of emitting end/start pairs.
MEETING_GRACE_SECS = 120

# Protects duration sums from clock jumps / laptop sleep without lock events.
MAX_SLICE_SECS = Config.ACTIVITY_HEARTBEAT_SECS + Config.WINDOW_POLL_SECS * 2


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


# ── macOS browser-tab fallback ────────────────────────────────────────────────
# Quartz window titles require Screen Recording permission. Browsers expose
# the active tab via AppleScript instead (prompts once for Automation access).

_CHROMIUM_TAB_SCRIPT = (
    'tell application "{app}" to if (count of windows) > 0 then '
    'get (title of active tab of front window) & "\\n" & (URL of active tab of front window)'
)
_SAFARI_TAB_SCRIPT = (
    'tell application "Safari" to if (count of windows) > 0 then '
    'get (name of current tab of front window) & "\\n" & (URL of current tab of front window)'
)

# Browsers that speak the Chrome AppleScript dictionary
_CHROMIUM_SCRIPTABLE = frozenset({
    "google chrome", "microsoft edge", "brave browser", "arc", "vivaldi", "opera",
})

_applescript_fails: dict[str, int] = {}


def _macos_browser_tab(app: str) -> tuple[str, str]:
    """(tab_title, tab_url) of the frontmost browser via AppleScript; ('','') on failure."""
    key = app.lower()
    if _applescript_fails.get(key, 0) >= 3:
        return "", ""
    if key == "safari":
        script = _SAFARI_TAB_SCRIPT
    elif key in _CHROMIUM_SCRIPTABLE:
        script = _CHROMIUM_TAB_SCRIPT.format(app=app)
    else:
        return "", ""
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            _applescript_fails[key] = _applescript_fails.get(key, 0) + 1
            if _applescript_fails[key] == 3:
                log.warning("AppleScript tab lookup for %s keeps failing — "
                            "check Automation permission for TimeTracker: %s",
                            app, out.stderr.strip())
            return "", ""
        _applescript_fails[key] = 0
        title, _, url = out.stdout.rstrip("\n").partition("\n")
        return title.strip(), url.strip()
    except subprocess.TimeoutExpired:
        # Likely the one-time Automation permission prompt blocking osascript —
        # don't count toward the give-up cap, the user may still grant it.
        log.debug("AppleScript tab lookup timed out for %s", app)
        return "", ""
    except Exception as e:
        _applescript_fails[key] = _applescript_fails.get(key, 0) + 1
        log.debug("AppleScript tab lookup failed for %s: %s", app, e)
        return "", ""


# ── Context classification ────────────────────────────────────────────────────

def _is_browser(app_name: str) -> bool:
    a = app_name.lower()
    return any(b in a for b in Config.BROWSER_APPS)


def _is_meeting_context(app_name: str, title: str) -> bool:
    """True for native meeting apps OR a browser tab on a meeting site."""
    a = app_name.lower()
    if any(m in a for m in MEETING_APPS):
        return True
    if "slack" in a and "huddle" in title.lower():
        return True
    if _is_browser(app_name):
        t = title.lower()
        return any(kw in t for kw in Config.MEETING_BROWSER_TITLES)
    return False


def _is_media_context(app_name: str, title: str) -> bool:
    """True for native media apps OR a browser tab playing media."""
    a = app_name.lower()
    if any(m in a for m in Config.MEDIA_APPS):
        return True
    if _is_browser(app_name):
        t = title.lower()
        return any(kw in t for kw in Config.MEDIA_BROWSER_TITLES)
    return False


class WindowWatcher:
    def __init__(self, shared: dict):
        self._shared       = shared
        self._db           = shared["db"]
        self._pause        = shared["pause_event"]
        self._stop         = shared["stop_event"]
        self._interval     = Config.WINDOW_POLL_SECS
        self._last_app     = ""
        self._last_title   = ""
        self._slice_start  = time.time()
        self._in_meeting        = False
        self._meeting_start     = 0.0
        self._meeting_app       = ""
        self._meeting_title     = ""
        self._meeting_last_seen = 0.0

    def run(self):
        log.info("WindowWatcher started (poll=%ds heartbeat=%ds)",
                 self._interval, Config.ACTIVITY_HEARTBEAT_SECS)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                # One bad poll must never kill the thread — window tracking,
                # meeting detection and idle media context all depend on it.
                log.warning("Window poll failed — will retry next cycle: %s", e)
            time.sleep(self._interval)
        log.info("WindowWatcher stopped")

    def _tick(self):
        now = time.time()

        if self._pause.is_set():
            # Locked/paused time must not land in the next slice.
            self._slice_start = now
            return

        app, title = _get_active_window()

        # Defense-in-depth: if the foreground window is the OS lock/login
        # screen (loginwindow, LockApp, etc.), treat it exactly like a pause.
        # This catches the rare race window where the lock detector hasn't
        # fired yet (e.g., agent just started while screen was already locked).
        if app.lower() in LOCK_SCREEN_APPS:
            self._slice_start = now
            return

        # macOS without Screen Recording permission → empty titles. For
        # browsers, the active tab is available via AppleScript instead.
        if sys.platform == "darwin" and not title and _is_browser(app):
            tab_title, _tab_url = _macos_browser_tab(app)
            if tab_title:
                title = tab_title

        is_idle = bool(self._shared.get("is_idle"))

        if app != self._last_app or title != self._last_title:
            # Close out the slice for the previous window
            if self._last_app and not is_idle:
                self._emit_slice(self._last_app, self._last_title, now)
            self._slice_start = now
            self._last_app    = app
            self._last_title  = title
            self._shared["current_app"]   = app
            self._shared["current_title"] = title
        elif is_idle:
            # Idle time never accumulates into a window slice
            self._slice_start = now
        elif now - self._slice_start >= Config.ACTIVITY_HEARTBEAT_SECS:
            # Heartbeat: same window, emit the elapsed slice so activity
            # logs flow continuously even without a window switch.
            self._emit_slice(app, title, now)
            self._slice_start = now

        self._update_meeting_state(app, title, now)

    def _emit_slice(self, app: str, title: str, now: float):
        duration = min(int(now - self._slice_start), MAX_SLICE_SECS)
        if duration <= 0 or not app:
            return
        self._db.insert_event({
            "type":     "window_change",
            "app":      app,
            "title":    title,
            "duration": duration,
            "ts":       int(now),
        })
        self._shared["sync_trigger"].set()

    def _update_meeting_state(self, app: str, title: str, now: float):
        in_meeting_ctx = _is_meeting_context(app, title)
        if in_meeting_ctx:
            self._meeting_last_seen = now

        if in_meeting_ctx and not self._in_meeting:
            self._in_meeting    = True
            self._meeting_start = now
            self._meeting_app   = app
            self._meeting_title = title
            self._db.insert_event({
                "type":  "meeting_start",
                "app":   app,
                "title": title,
                "ts":    int(now),
            })
            self._shared["sync_trigger"].set()
            log.info("Meeting started: %s — %s", app, title)

        elif self._in_meeting and not in_meeting_ctx:
            # Grace period: brief switches away (notes, Slack) don't end it.
            if now - self._meeting_last_seen > MEETING_GRACE_SECS:
                self._in_meeting = False
                meeting_dur      = int(now - self._meeting_start)
                self._db.insert_event({
                    "type":     "meeting_end",
                    "app":      self._meeting_app,
                    "title":    self._meeting_title,
                    "duration": meeting_dur,
                    "ts":       int(now),
                })
                self._shared["sync_trigger"].set()
                log.info("Meeting ended: %s (%ds)", self._meeting_app, meeting_dur)

        self._shared["in_meeting"]   = self._in_meeting
        self._shared["media_active"] = _is_media_context(app, title)
