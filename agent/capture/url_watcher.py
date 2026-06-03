"""
UrlWatcher — polls browser history databases (Chrome, Firefox, Edge)
every URL_POLL_SECS and inserts new URLs into the event queue.
Tracks the last-seen timestamp per browser to avoid duplicates.
"""

import sys
import time
import sqlite3
import shutil
import logging
import tempfile
from pathlib import Path
from config import Config

log = logging.getLogger("url_watcher")


def _chrome_paths() -> list[Path]:
    paths = []
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local"
        for browser in ["Google/Chrome", "Microsoft/Edge", "BraveSoftware/Brave-Browser"]:
            p = base / browser / "User Data" / "Default" / "History"
            if p.exists():
                paths.append(p)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        for browser in ["Google/Chrome", "Microsoft Edge", "BraveSoftware/Brave-Browser"]:
            p = base / browser / "Default" / "History"
            if p.exists():
                paths.append(p)
    else:
        for browser_dir in ["google-chrome", "chromium", "microsoft-edge-stable"]:
            p = Path.home() / ".config" / browser_dir / "Default" / "History"
            if p.exists():
                paths.append(p)
    return paths


def _firefox_paths() -> list[Path]:
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:
        base = Path.home() / ".mozilla" / "firefox"
    paths = []
    if base.exists():
        for profile in base.iterdir():
            p = profile / "places.sqlite"
            if p.exists():
                paths.append(p)
    return paths


def _query_chrome(db_path: Path, since_ts: float) -> list[tuple[str, float]]:
    """Returns [(url, unix_timestamp), ...] for visits after since_ts."""
    results = []
    try:
        # Copy to temp file to avoid "database locked" errors from the live browser
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            shutil.copy2(db_path, tmp.name)
            conn = sqlite3.connect(tmp.name)
            # Chrome stores time as microseconds since 1601-01-01
            offset = 11644473600  # seconds between 1601 and 1970
            rows = conn.execute(
                """SELECT url, last_visit_time / 1000000.0 - ? as ts
                   FROM urls
                   WHERE last_visit_time / 1000000.0 - ? > ?
                   ORDER BY last_visit_time ASC LIMIT 200""",
                (offset, offset, since_ts),
            ).fetchall()
            conn.close()
        results = [(r[0], r[1]) for r in rows]
    except Exception as e:
        log.debug("Chrome history read error: %s", e)
    return results


def _query_firefox(db_path: Path, since_ts: float) -> list[tuple[str, float]]:
    results = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            shutil.copy2(db_path, tmp.name)
            conn = sqlite3.connect(tmp.name)
            rows = conn.execute(
                """SELECT url, last_visit_date / 1000000.0 as ts
                   FROM moz_places
                   WHERE last_visit_date IS NOT NULL
                   AND last_visit_date / 1000000.0 > ?
                   ORDER BY last_visit_date ASC LIMIT 200""",
                (since_ts,),
            ).fetchall()
            conn.close()
        results = [(r[0], r[1]) for r in rows]
    except Exception as e:
        log.debug("Firefox history read error: %s", e)
    return results


class UrlWatcher:
    def __init__(self, shared: dict):
        self._db        = shared["db"]
        self._pause     = shared["pause_event"]
        self._stop      = shared["stop_event"]
        self._last_seen: dict[str, float] = {}  # path → last timestamp

    def run(self):
        log.info("UrlWatcher started")
        while not self._stop.is_set():
            if not self._pause.is_set():
                self._poll()
            for _ in range(Config.URL_POLL_SECS):
                if self._stop.is_set():
                    break
                time.sleep(1)
        log.info("UrlWatcher stopped")

    def _poll(self):
        for path in _chrome_paths():
            key    = str(path)
            since  = self._last_seen.get(key, time.time() - 300)
            visits = _query_chrome(path, since)
            self._emit(visits, key)

        for path in _firefox_paths():
            key    = str(path)
            since  = self._last_seen.get(key, time.time() - 300)
            visits = _query_firefox(path, since)
            self._emit(visits, key)

    def _emit(self, visits: list[tuple[str, float]], key: str):
        for url, ts in visits:
            self._db.insert_event({
                "type": "url_visit",
                "url":  url,
                "duration": 0,
            })
            self._last_seen[key] = max(self._last_seen.get(key, 0), ts)
