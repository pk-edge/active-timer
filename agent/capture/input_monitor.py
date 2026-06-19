"""
InputMonitor — detects user idle state with smart media/meeting exceptions.

Rules:
  - If no mouse/keyboard input for IDLE_THRESHOLD_SECS (5 min) → emit idle_start.
  - Exception: if the user is in a meeting or watching media (native app OR
    browser tab — Zoom, Teams, Google Meet, YouTube, Netflix…) the timer keeps
    running even without input — the user is watching/listening. The primary
    signal is shared["in_meeting"] / shared["media_active"] published by
    WindowWatcher; a local app/title check is kept as fallback.
  - When input resumes after idle → emit idle_end.
  - Publishes shared["is_idle"] so WindowWatcher excludes idle time from
    activity slices.

macOS: polls CGEventSourceSecondsSinceLastEventType (zero PyObjC allocation).
Other: pynput keyboard + mouse listeners.
"""

import sys
import time
import threading
import logging

from config import Config

log = logging.getLogger("input_monitor")


def _is_media_or_meeting(shared: dict) -> bool:
    """Return True when the user is in a meeting or watching media — timer stays active."""
    # Primary: state published by WindowWatcher (covers browser meetings too)
    if shared.get("in_meeting") or shared.get("media_active"):
        return True

    # Fallback: classify the current window ourselves
    app   = (shared.get("current_app")   or "").lower()
    title = (shared.get("current_title") or "").lower()

    if any(m in app for m in Config.MEDIA_APPS):
        return True

    if any(b in app for b in Config.BROWSER_APPS):
        if any(kw in title for kw in Config.MEDIA_BROWSER_TITLES):
            return True
        if any(kw in title for kw in Config.MEETING_BROWSER_TITLES):
            return True

    return False


def _should_mark_idle(idle_secs: float, exempt: bool, idle_threshold: int) -> bool:
    """Decide whether to transition into idle.

    - Below the idle threshold → never idle.
    - Past the threshold and NOT in a meeting/media context → idle.
    - In a meeting/media context → NEVER idle (the user may be watching/listening,
      so live meeting/video time is never cut by inactivity). A walk-away during a
      meeting is bounded by screen lock / display sleep instead — by design.
    """
    if idle_secs < idle_threshold:
        return False
    return not exempt


class InputMonitor:
    def __init__(self, shared: dict):
        self._db      = shared["db"]
        self._shared  = shared
        self._pause   = shared["pause_event"]
        self._stop    = shared["stop_event"]
        self._is_idle = False
        self._lock    = threading.Lock()
        shared["is_idle"] = False

    def run(self):
        log.info("InputMonitor started (threshold=%ds)", Config.IDLE_THRESHOLD_SECS)
        if sys.platform == "darwin":
            self._run_macos()
        else:
            self._run_pynput()
        log.info("InputMonitor stopped")

    def _set_idle(self, idle: bool, duration: int = 0) -> None:
        """Update idle state + shared flag and queue the idle event."""
        self._is_idle = idle
        self._shared["is_idle"] = idle
        self._db.insert_event({
            "type":     "idle_start" if idle else "idle_end",
            "duration": duration,
        })
        self._shared["sync_trigger"].set()

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
            try:
                idle_secs = CGEventSourceSecondsSinceLastEventType(
                    kCGEventSourceStateHIDSystemState,
                    kCGAnyInputEventType,
                )
                with self._lock:
                    if not self._is_idle and _should_mark_idle(
                        idle_secs, _is_media_or_meeting(self._shared),
                        Config.IDLE_THRESHOLD_SECS,
                    ):
                        self._set_idle(True, int(idle_secs))
                        log.debug("Idle started (%.0fs without input)", idle_secs)
                    elif self._is_idle and idle_secs < Config.IDLE_THRESHOLD_SECS:
                        self._set_idle(False)
                        log.debug("Idle ended")
            except Exception as e:
                log.warning("Idle check failed — will retry: %s", e)

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
        try:
            with self._lock:
                self._last_input = time.time()
                if self._is_idle:
                    self._set_idle(False)
                    log.debug("Idle ended (input received)")
        except Exception as e:
            log.warning("Idle-end handling failed: %s", e)

    def _idle_checker(self):
        while not self._stop.is_set():
            time.sleep(1)
            if self._pause.is_set():
                continue
            try:
                with self._lock:
                    elapsed = time.time() - self._last_input
                    if not self._is_idle and _should_mark_idle(
                        elapsed, _is_media_or_meeting(self._shared),
                        Config.IDLE_THRESHOLD_SECS,
                    ):
                        self._set_idle(True, int(elapsed))
                        log.debug("Idle started (%.0fs without input)", elapsed)
            except Exception as e:
                log.warning("Idle check failed — will retry: %s", e)
