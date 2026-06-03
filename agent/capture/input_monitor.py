"""
InputMonitor — detects user idle without pynput.

macOS: polls CGEventSourceSecondsSinceLastEventType once per second —
       zero PyObjC object allocation, negligible CPU.
Other: falls back to pynput keyboard+click/scroll listeners.

Never records key content.
"""

import sys
import time
import threading
import logging

from config import Config

log = logging.getLogger("input_monitor")


class InputMonitor:
    def __init__(self, shared: dict):
        self._db      = shared["db"]
        self._pause   = shared["pause_event"]
        self._stop    = shared["stop_event"]
        self._is_idle = False
        self._lock    = threading.Lock()

    def run(self):
        log.info("InputMonitor started")
        if sys.platform == "darwin":
            self._run_macos()
        else:
            self._run_pynput()
        log.info("InputMonitor stopped")

    # ── macOS — single C call per second, no PyObjC overhead ─────────────────

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
                    self._is_idle = True
                    self._db.insert_event({
                        "type":     "idle_start",
                        "duration": int(idle_secs),
                    })
                elif self._is_idle and idle_secs < Config.IDLE_THRESHOLD_SECS:
                    self._is_idle = False
                    self._db.insert_event({"type": "idle_end", "duration": 0})

    # ── Other platforms — pynput ──────────────────────────────────────────────

    def _run_pynput(self):
        self._last_input = time.time()
        try:
            from pynput import keyboard, mouse
            kb = keyboard.Listener(on_press=self._on_input)
            ms = mouse.Listener(
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

    def _idle_checker(self):
        while not self._stop.is_set():
            time.sleep(1)
            if self._pause.is_set():
                continue
            with self._lock:
                elapsed = time.time() - self._last_input
                if not self._is_idle and elapsed >= Config.IDLE_THRESHOLD_SECS:
                    self._is_idle = True
                    self._db.insert_event({
                        "type":     "idle_start",
                        "duration": int(elapsed),
                    })
