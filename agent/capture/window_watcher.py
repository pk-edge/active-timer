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
from meeting_detect import detect_presence, MeetingSessionTracker

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

# A meeting stays "open" until the call has been absent this long — covers brief
# tab/app switches mid-call (checking notes, replying on Slack) and any
# locked/away gap, which ends the session at the last in-call moment. Several
# poll cycles so a single missed poll never flickers the session.
MEETING_CONFIRM_SECS = 25

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


# ── Meeting-presence collectors (thin OS shell over the pure detect core) ─────

def _list_windows() -> list[dict]:
    """[{'owner','title'}] for visible, normal-layer windows. Best-effort; the
    poll loop tolerates a return of []. macOS uses Quartz, Windows EnumWindows."""
    try:
        if sys.platform == "darwin":
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )
            wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
            out = []
            for w in wins:
                if w.get("kCGWindowLayer") != 0:
                    continue  # skip menubar/dock/overlay layers
                out.append({
                    "owner": w.get("kCGWindowOwnerName") or "",
                    "title": w.get("kCGWindowName") or "",
                })
            return out
        if sys.platform == "win32":
            return _win_list_windows()
    except Exception as e:
        log.debug("_list_windows error: %s", e)
    return []


def _win_list_windows() -> list[dict]:
    """Windows: enumerate visible top-level windows with their owning process."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    out: list[dict] = []
    try:
        import psutil
    except Exception:
        psutil = None

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value or ""
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            owner = ""
            if psutil is not None:
                try:
                    owner = psutil.Process(pid.value).name().replace(".exe", "")
                except Exception:
                    owner = ""
            out.append({"owner": owner, "title": title})
        except Exception:
            pass  # one bad window must not abort the whole enumeration
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception as e:
        log.debug("EnumWindows failed: %s", e)
    return out


def _browser_tabs(windows: list[dict]) -> list[dict]:
    """macOS only: active tab (url,title) of each *running* scriptable browser.

    Gated on browsers that actually have a window open — querying a browser that
    isn't running would make AppleScript launch it. Windows/Linux return []: live
    tab URLs aren't reliably available there, so browser meetings on those OSes
    rely on native apps; this is the accepted 'macOS-strong browser' tradeoff."""
    tabs: list[dict] = []
    if sys.platform != "darwin":
        return tabs
    seen = set()
    for w in windows:
        owner = w.get("owner") or ""
        if owner in seen or not _is_browser(owner):
            continue
        seen.add(owner)
        title, url = _macos_browser_tab(owner)
        if url:
            tabs.append({"url": url, "title": title})
    return tabs


def _collect_meeting_signals() -> tuple[list[dict], list[dict]]:
    """(windows, tabs) for detect_presence. Never raises. Skips the costlier
    browser-tab enumeration when a native call window is already present."""
    windows = _list_windows()
    if detect_presence(windows, []) is not None:
        return windows, []
    return windows, _browser_tabs(windows)


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
        # Session/presence meeting detection (see meeting_detect.py). Replaces the
        # old foreground-only grace model: scans ALL windows + browser tabs so a
        # backgrounded call still counts, and survives brief flicker.
        self._meeting_tracker = MeetingSessionTracker(MEETING_CONFIRM_SECS)

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

        self._update_meetings(now, app, title)

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

    def _update_meetings(self, now: float, app: str, title: str):
        """Feed the current call presence to the session tracker and persist any
        start/end it emits. `app`/`title` are the foreground window, used only for
        the media-active flag (meeting detection scans all windows + tabs)."""
        windows, tabs = _collect_meeting_signals()
        presence = detect_presence(windows, tabs)

        for ev in self._meeting_tracker.update(presence, now):
            if ev["type"] == "meeting_start":
                self._db.insert_event({
                    "type":  "meeting_start",
                    "app":   ev["platform"],
                    "title": ev["title"],
                    "ts":    int(ev["ts"]),
                })
                log.info("Meeting started: %s — %s", ev["platform"], ev["title"])
            else:  # meeting_end
                self._db.insert_event({
                    "type":     "meeting_end",
                    "app":      ev["platform"],
                    "title":    ev["title"],
                    "duration": int(ev["duration"]),
                    "ts":       int(ev["ts"]),
                })
                log.info("Meeting ended: %s (%ds)", ev["platform"], ev["duration"])
            self._shared["sync_trigger"].set()

        self._shared["in_meeting"]   = self._meeting_tracker.in_meeting
        self._shared["media_active"] = _is_media_context(app, title)
