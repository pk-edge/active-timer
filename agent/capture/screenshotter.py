"""
Screenshotter — captures the full screen every SCREENSHOT_INTERVAL seconds.
Saves compressed PNG locally, then inserts a screenshot record into the
local DB queue for the sync worker to upload.
"""

import time
import logging
from pathlib import Path
from datetime import datetime, timezone

from config import Config

log = logging.getLogger("screenshotter")


class Screenshotter:
    def __init__(self, shared: dict):
        self._db    = shared["db"]
        self._pause = shared["pause_event"]
        self._stop  = shared["stop_event"]

    def run(self):
        log.info("Screenshotter started (interval=%ds)", Config.SCREENSHOT_INTERVAL)
        while not self._stop.is_set():
            # Sleep first so we don't screenshot immediately on unlock
            for _ in range(Config.SCREENSHOT_INTERVAL):
                if self._stop.is_set():
                    return
                time.sleep(1)

            if self._pause.is_set():
                continue

            self._capture()

        log.info("Screenshotter stopped")

    def _capture(self):
        try:
            import mss
            import mss.tools

            ts       = datetime.now(timezone.utc)
            filename = ts.strftime("%Y%m%d_%H%M%S") + ".png"
            path     = Config.SCREENSHOT_DIR / filename

            with mss.mss() as sct:
                # Capture primary monitor (monitor 1)
                monitor = sct.monitors[1]
                img     = sct.grab(monitor)
                mss.tools.to_png(img.rgb, img.size, output=str(path))

            size = path.stat().st_size
            self._db.insert_screenshot(str(path), size)
            log.debug("Screenshot saved: %s (%d bytes)", filename, size)

        except Exception as e:
            log.error("Screenshot failed: %s", e)
