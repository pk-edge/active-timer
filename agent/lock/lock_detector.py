"""
LockDetector — cross-platform screen lock/unlock detection.

  Windows : WTSRegisterSessionNotification (win32api)
  macOS   : NSDistributedNotificationCenter (pyobjc)
  Linux   : D-Bus org.freedesktop.login1 LockedHint property

on_lock and on_unlock are callables fired on the respective events.
"""

import sys
import threading
import logging

log = logging.getLogger("lock_detector")


class LockDetector:
    def __init__(self, on_lock, on_unlock):
        self._on_lock   = on_lock
        self._on_unlock = on_unlock
        self._thread    = None

    def start(self):
        if sys.platform == "win32":
            self._thread = threading.Thread(
                target=self._run_windows, daemon=True, name="LockDetector"
            )
        elif sys.platform == "darwin":
            self._thread = threading.Thread(
                target=self._run_macos, daemon=True, name="LockDetector"
            )
        else:
            self._thread = threading.Thread(
                target=self._run_linux, daemon=True, name="LockDetector"
            )
        self._thread.start()
        log.info("LockDetector started (%s)", sys.platform)

    # ── Windows ────────────────────────────────────────────────────────────

    @staticmethod
    def _windows_is_locked() -> bool:
        """Return True if the Windows workstation is currently locked.

        OpenInputDesktop returns NULL (0) when the interactive desktop is
        not the current thread's desktop — i.e. when the screen is locked.
        """
        try:
            import ctypes
            DESKTOP_SWITCHDESKTOP = 0x0100
            hDesk = ctypes.windll.user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
            if hDesk:
                ctypes.windll.user32.CloseDesktop(hDesk)
                return False
            return True
        except Exception:
            return False

    def _run_windows(self):
        try:
            import win32api
            import win32con
            import win32gui
            import win32ts

            WM_WTSSESSION_CHANGE  = 0x02B1
            WTS_SESSION_LOCK      = 0x7
            WTS_SESSION_UNLOCK    = 0x8

            # Check initial state BEFORE registering for session-change events.
            # WTSRegisterSessionNotification only fires on transitions, so if
            # the screen is already locked when the agent starts, pause_event
            # would never be set without this explicit check.
            if self._windows_is_locked():
                log.info("Screen already locked at agent start (Windows)")
                self._on_lock()

            class _Listener:
                def __init__(self, on_lock, on_unlock):
                    self._on_lock   = on_lock
                    self._on_unlock = on_unlock

                def WndProc(self, hwnd, msg, wparam, lparam):
                    if msg == WM_WTSSESSION_CHANGE:
                        if wparam == WTS_SESSION_LOCK:
                            self._on_lock()
                        elif wparam == WTS_SESSION_UNLOCK:
                            self._on_unlock()
                    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

            listener = _Listener(self._on_lock, self._on_unlock)
            wc        = win32gui.WNDCLASS()
            wc.lpszClassName = "TTLockDetector"
            wc.lpfnWndProc   = listener.WndProc
            win32gui.RegisterClass(wc)
            hwnd = win32gui.CreateWindow(
                wc.lpszClassName, "", 0, 0, 0, 0, 0, 0, 0, None, None
            )
            win32ts.WTSRegisterSessionNotification(hwnd, win32ts.NOTIFY_FOR_THIS_SESSION)
            win32gui.PumpMessages()
        except Exception as e:
            log.error("Windows lock detector failed: %s", e)
            self._fallback_poll()

    # ── macOS ──────────────────────────────────────────────────────────────

    def _run_macos(self):
        """
        Poll CGSessionCopyCurrentDictionary every 2 seconds.
        Pure Python sleep loop — zero PyObjC run-loop overhead.
        """
        import time
        try:
            from Quartz import CGSessionCopyCurrentDictionary
        except ImportError:
            log.warning("Quartz unavailable — macOS lock detection disabled")
            return

        def _read_locked() -> bool:
            try:
                session = CGSessionCopyCurrentDictionary()
                return bool(session.get("CGSSessionScreenIsLocked", 0)) if session else False
            except Exception:
                return False

        # Check initial state BEFORE the first sleep so that if the agent
        # starts while the screen is already locked, pause_event is set
        # immediately — not 2 seconds later — preventing the WindowWatcher
        # from tracking loginwindow during that gap.
        def _fire(cb) -> None:
            # A failing handler (e.g. a locked queue.db) must not kill this thread:
            # it is the only thing that ever clears pause_event, so a dead detector
            # would leave every capture thread paused for good.
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log.warning("Lock/unlock handler failed — detector keeps running: %s", e)

        was_locked = _read_locked()
        if was_locked:
            log.info("Screen already locked at agent start")
            _fire(self._on_lock)

        log.info("LockDetector polling CGSession every 2 s")
        while True:
            time.sleep(2)
            locked = _read_locked()

            if locked and not was_locked:
                was_locked = True
                log.info("Screen locked")
                _fire(self._on_lock)
            elif not locked and was_locked:
                was_locked = False
                log.info("Screen unlocked")
                _fire(self._on_unlock)

    # ── Linux ──────────────────────────────────────────────────────────────

    def _run_linux(self):
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()

            on_lock   = self._on_lock
            on_unlock = self._on_unlock

            session_obj = bus.get_object(
                "org.freedesktop.login1",
                "/org/freedesktop/login1/session/auto",
            )

            # Check initial LockedHint BEFORE subscribing to changes so that
            # if the agent starts while the session is already locked, on_lock()
            # fires immediately rather than waiting for the next transition.
            try:
                props_iface = dbus.Interface(session_obj, "org.freedesktop.DBus.Properties")
                locked_hint = props_iface.Get("org.freedesktop.login1.Session", "LockedHint")
                if bool(locked_hint):
                    log.info("Screen already locked at agent start (Linux)")
                    on_lock()
            except Exception:
                pass

            def _props_changed(iface, changed, invalidated):
                if "LockedHint" in changed:
                    if bool(changed["LockedHint"]):
                        on_lock()
                    else:
                        on_unlock()

            session_obj.connect_to_signal(
                "PropertiesChanged",
                _props_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
            )

            loop = GLib.MainLoop()
            loop.run()

        except Exception as e:
            log.error("Linux lock detector failed: %s", e)
            self._fallback_poll()

    # ── Fallback ───────────────────────────────────────────────────────────

    def _fallback_poll(self):
        """
        Crude fallback for unsupported environments:
        polls screen-saver state every 5 seconds via xscreensaver-command.
        """
        import time
        import subprocess
        was_locked = False
        log.warning("Using fallback lock polling")
        while True:
            try:
                result = subprocess.run(
                    ["xscreensaver-command", "-time"],
                    capture_output=True, text=True, timeout=2
                )
                locked = "locked" in result.stdout.lower()
                if locked and not was_locked:
                    self._on_lock()
                elif not locked and was_locked:
                    self._on_unlock()
                was_locked = locked
            except Exception:
                pass
            time.sleep(5)
