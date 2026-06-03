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

    def _run_windows(self):
        try:
            import win32api
            import win32con
            import win32gui
            import win32ts

            WM_WTSSESSION_CHANGE  = 0x02B1
            WTS_SESSION_LOCK      = 0x7
            WTS_SESSION_UNLOCK    = 0x8

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

        was_locked = False
        log.info("LockDetector polling CGSession every 2 s")
        while True:
            time.sleep(2)
            try:
                session = CGSessionCopyCurrentDictionary()
                locked = bool(
                    session.get("CGSSessionScreenIsLocked", 0)
                ) if session else False
            except Exception:
                locked = False

            if locked and not was_locked:
                was_locked = True
                log.info("Screen locked")
                self._on_lock()
            elif not locked and was_locked:
                was_locked = False
                log.info("Screen unlocked")
                self._on_unlock()

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

            def _props_changed(iface, changed, invalidated):
                if "LockedHint" in changed:
                    if bool(changed["LockedHint"]):
                        on_lock()
                    else:
                        on_unlock()

            session_obj = bus.get_object(
                "org.freedesktop.login1",
                "/org/freedesktop/login1/session/auto",
            )
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
