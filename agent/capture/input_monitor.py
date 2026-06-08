"""
InputMonitor — detects user idle state with smart media/meeting exceptions.

Rules:
  - If no mouse/keyboard input for IDLE_THRESHOLD_SECS (5 min) → emit idle_start.
  - Exception: if the active app is a known meeting or media-streaming app
    (Zoom, Teams, YouTube in browser, Netflix…) the timer keeps running even
    without input — the user is watching/listening.
  - When input resumes after idle → emit idle_end.

macOS: polls CGEventSourceSecondsSinceLastEventType (zero PyObjC allocation).
Other: pynput keyboard + mouse listeners.
"""

import sys
import time
import threading
import logging

from config import Config

log = logging.getLogger("input_monitor")

BROWSER_APPS = frozenset({
    "chrome", "google chrome", "firefox", "safari",
    "microsoft edge", "brave browser", "arc", "opera",
})


def _is_media_or_meeting(shared: dict) -> bool:
    """Return True when the user is in a meeting or watching media — timer stays active."""
    app   = (shared.get("current_app")   or "").lower()
    title = (shared.get("current_title") or "").lower()

    # Direct meeting/media app
    if any(m in app for m in Config.MEDIA_APPS):
        return True

    # Browser tab with media/meeting content
    if any(b in app for b in BROWSER_APPS):
        if any(kw in title for kw in Config.MEDIA_BROWSER_TITLES):
            return True

    return False


class InputMonitor:
    def __init__(self, shared: dict):
        self._db      = shared["db"]
        self._shared  = shared
        self._pause   = shared["pause_event"]
        self._stop    = shared["stop_event"]
        self._is_idle = False
        self._lock    = threading.Lock()

    def run(self):
        log.info("InputMonitor started (threshold=%ds)", Config.IDLE_THRESHOLD_SECS)
        if sys.platform == "darwin":
            self._run_macos()
        else:
            self._run_pynput()
        log.info("InputMonitor stopped")

    # ── macOS ─────────────────────────────────────────────────────────────────

    def _run_macos(self):
        try:
            from Quartz import (
                CGEventSourceSecondsSinceLastEventType,
                kCGEventSourceStateHIDSystemState,
                kCGAnyInputEventType,
            )
        except ImportError:
            log.warning("Quartz not available — falling back to pynput")
            self._run_pynput()
            return

        log.info("Using CGEventSource idle polling (low-CPU)")
        while not self._stop.is_set():
            time.sleep(1)
            if self._pause.is_set():
                continue
            idle_secs = CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState,
                kCGAnyInputEventType,
            )
            with self._lock:
                if not self._is_idle and idle_secs >= Config.IDLE_THRESHOLD_SECS:
                    # Only go idle if NOT in an active media/meeting context
                    if not _is_media_or_meeting(self._shared):
                        self._is_idle = True
                        self._db.insert_event({
                            "type":     "idle_start",
                            "duration": int(idle_secs),
                        })
                        log.debug("Idle started (%.0fs without input)", idle_secs)
                elif self._is_idle and idle_secs < Config.IDLE_THRESHOLD_SECS:
                    self._is_idle = False
                    self._db.insert_event({"type": "idle_end", "duration": 0})
                    log.debug("Idle ended")

    # ── Windows / Linux — pynput ──────────────────────────────────────────────

    def _run_pynput(self):
        self._last_input = time.time()
        try:
            from pynput import keyboard, mouse
            kb = keyboard.Listener(on_press=self._on_input)
            ms = mouse.Listener(
                on_move=self._on_input,
                on_click=self._on_input,
                on_scroll=self._on_input,
            )
            kb.start()
            ms.start()
            log.info("pynput listeners active")
            self._idle_checker()
            kb.stop()
            ms.stop()
        except ImportError:
            log.warning("pynput not installed — idle detection disabled")
            self._idle_checker()
        except Exception as e:
            log.warning("pynput failed (%s) — idle detection disabled", e)
            self._idle_checker()

    def _on_input(self, *_):
        if self._pause.is_set():
            return
        with self._lock:
            self._last_input = time.time()
            if self._is_idle:
                self._is_idle = False
                self._db.insert_event({"type": "idle_end", "duration": 0})
                log.debug("Idle ended (input received)")

    def _idle_checker(self):
        while not self._stop.is_set():
            time.sleep(1)
            if self._pause.is_set():
                continue
            with self._lock:
                elapsed = time.time() - self._last_input
                if not self._is_idle and elapsed >= Config.IDLE_THRESHOLD_SECS:
                    if not _is_media_or_meeting(self._shared):
                        self._is_idle = True
                        self._db.insert_event({
                            "type":     "idle_start",
                            "duration": int(elapsed),
                        })
                        log.debug("Idle started (%.0fs without input)", elapsed)
