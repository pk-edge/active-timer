"""
UrlWatcher — polls browser history databases (Chrome, Firefox, Edge, Brave, Arc, Safari)
every URL_POLL_SECS and emits url_visit events with the *actual* visit timestamp (UTC Unix).

New in this version:
  - Uses real visit time from browser history (not time.time() at poll moment).
  - Captures page title from browser history where available.
  - Checks each URL against the agent's local blocked-URL policy; emits
    url_blocked events for violations (for the admin dashboard).
  - Supports Chrome, Edge, Brave, Arc (Chromium-based), Firefox, and Safari.
  - Classifies URLs into categories: meeting / media / social / productivity / other.
"""

import sys
import time
import sqlite3
import shutil
import logging
import tempfile
import re
from pathlib import Path
from config import Config

log = logging.getLogger("url_watcher")

# ── URL classification ────────────────────────────────────────────────────────

MEETING_DOMAINS  = {"meet.google.com", "zoom.us", "teams.microsoft.com",
                    "webex.com", "whereby.com", "around.co", "loom.com"}
MEDIA_DOMAINS    = {"youtube.com", "netflix.com", "spotify.com", "twitch.tv",
                    "vimeo.com", "hulu.com", "disneyplus.com", "primevideo.com",
                    "soundcloud.com", "tidal.com", "music.apple.com"}
SOCIAL_DOMAINS   = {"facebook.com", "twitter.com", "x.com", "instagram.com",
                    "tiktok.com", "reddit.com", "linkedin.com", "snapchat.com"}
PRODUCTIVITY_DOMAINS = {
    "docs.google.com", "sheets.google.com", "slides.google.com",
    "drive.google.com", "notion.so", "atlassian.net", "jira", "confluence",
    "github.com", "gitlab.com", "stackoverflow.com", "figma.com",
    "slack.com", "asana.com", "trello.com", "monday.com",
}


def _classify_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        host = re.sub(r"^www\.", "", host.lower())
        if any(d in host for d in MEETING_DOMAINS):     return "meeting"
        if any(d in host for d in MEDIA_DOMAINS):       return "media"
        if any(d in host for d in SOCIAL_DOMAINS):      return "social"
        if any(d in host for d in PRODUCTIVITY_DOMAINS): return "productivity"
    except Exception:
        pass
    return "other"


def _extract_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return re.sub(r"^www\.", "", (urlparse(url).hostname or "").lower())
    except Exception:
        return ""


# ── Browser history paths ─────────────────────────────────────────────────────

def _chromium_paths() -> list[Path]:
    """Return history DB paths for all Chromium-based browsers."""
    paths = []
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local"
        profiles = [
            "Google/Chrome", "Microsoft/Edge",
            "BraveSoftware/Brave-Browser", "Company/Arc",
        ]
        for profile in profiles:
            for sub in ["Default", *[f"Profile {i}" for i in range(1, 6)]]:
                p = base / profile / "User Data" / sub / "History"
                if p.exists():
                    paths.append(p)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        profiles = [
            "Google/Chrome", "Microsoft Edge",
            "BraveSoftware/Brave-Browser", "Arc/User Data",
        ]
        for profile in profiles:
            for sub in ["Default", *[f"Profile {i}" for i in range(1, 6)]]:
                p = base / profile / sub / "History"
                if p.exists():
                    paths.append(p)
        # Arc stores differently
        arc = base / "Arc/User Data/Default/History"
        if arc.exists() and arc not in paths:
            paths.append(arc)
    else:
        for browser_dir in ["google-chrome", "chromium", "microsoft-edge-stable", "brave-browser"]:
            for sub in ["Default", *[f"Profile {i}" for i in range(1, 4)]]:
                p = Path.home() / ".config" / browser_dir / sub / "History"
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


def _safari_paths() -> list[Path]:
    if sys.platform != "darwin":
        return []
    p = Path.home() / "Library" / "Safari" / "History.db"
    return [p] if p.exists() else []


# ── Query helpers ─────────────────────────────────────────────────────────────

def _copy_locked_db(db_path: Path) -> str | None:
    """Copy a browser's live DB to a temp file to avoid SQLITE_BUSY."""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        shutil.copy2(db_path, tmp.name)
        return tmp.name
    except Exception as e:
        log.debug("Could not copy %s: %s", db_path, e)
        return None


def _query_chromium(db_path: Path, since_ts: float) -> list[tuple[str, str, float]]:
    """Returns [(url, title, unix_ts), ...] for visits after since_ts."""
    tmp = _copy_locked_db(db_path)
    if not tmp:
        return []
    results = []
    try:
        conn = sqlite3.connect(tmp)
        OFFSET = 11644473600  # microseconds offset between 1601 and 1970
        rows = conn.execute(
            """
            SELECT u.url, u.title,
                   v.visit_time / 1000000.0 - ? AS ts
            FROM visits v
            JOIN urls u ON u.id = v.url
            WHERE v.visit_time / 1000000.0 - ? > ?
            ORDER BY v.visit_time ASC
            LIMIT 500
            """,
            (OFFSET, OFFSET, since_ts),
        ).fetchall()
        conn.close()
        results = [(r[0], r[1] or "", r[2]) for r in rows]
    except Exception as e:
        log.debug("Chromium history read error (%s): %s", db_path.name, e)
    finally:
        try:
            import os; os.remove(tmp)
        except Exception:
            pass
    return results


def _query_firefox(db_path: Path, since_ts: float) -> list[tuple[str, str, float]]:
    tmp = _copy_locked_db(db_path)
    if not tmp:
        return []
    results = []
    try:
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            """
            SELECT p.url, p.title, h.visit_date / 1000000.0 AS ts
            FROM moz_historyvisits h
            JOIN moz_places p ON p.id = h.place_id
            WHERE h.visit_date IS NOT NULL
              AND h.visit_date / 1000000.0 > ?
            ORDER BY h.visit_date ASC
            LIMIT 500
            """,
            (since_ts,),
        ).fetchall()
        conn.close()
        results = [(r[0], r[1] or "", r[2]) for r in rows]
    except Exception as e:
        log.debug("Firefox history read error: %s", e)
    finally:
        try:
            import os; os.remove(tmp)
        except Exception:
            pass
    return results


def _query_safari(db_path: Path, since_ts: float) -> list[tuple[str, str, float]]:
    tmp = _copy_locked_db(db_path)
    if not tmp:
        return []
    results = []
    try:
        conn = sqlite3.connect(tmp)
        # Safari stores time as seconds since 2001-01-01 (Cocoa epoch)
        COCOA_OFFSET = 978307200
        rows = conn.execute(
            """
            SELECT i.url, v.title, v.visit_time + ? AS ts
            FROM history_visits v
            JOIN history_items i ON i.id = v.history_item
            WHERE v.visit_time + ? > ?
            ORDER BY v.visit_time ASC
            LIMIT 500
            """,
            (COCOA_OFFSET, COCOA_OFFSET, since_ts),
        ).fetchall()
        conn.close()
        results = [(r[0], r[1] or "", r[2]) for r in rows]
    except Exception as e:
        log.debug("Safari history read error: %s", e)
    finally:
        try:
            import os; os.remove(tmp)
        except Exception:
            pass
    return results


# ── UrlWatcher ────────────────────────────────────────────────────────────────

class UrlWatcher:
    def __init__(self, shared: dict):
        self._db         = shared["db"]
        self._shared     = shared
        self._pause      = shared["pause_event"]
        self._stop       = shared["stop_event"]
        self._last_seen: dict[str, float] = {}

    def run(self):
        log.info("UrlWatcher started (poll=%ds)", Config.URL_POLL_SECS)
        while not self._stop.is_set():
            if not self._pause.is_set():
                self._poll()
            for _ in range(Config.URL_POLL_SECS):
                if self._stop.is_set():
                    break
                time.sleep(1)
        log.info("UrlWatcher stopped")

    def _poll(self):
        for path in _chromium_paths():
            key   = str(path)
            since = self._last_seen.get(key, time.time() - 120)
            self._emit(_query_chromium(path, since), key)

        for path in _firefox_paths():
            key   = str(path)
            since = self._last_seen.get(key, time.time() - 120)
            self._emit(_query_firefox(path, since), key)

        for path in _safari_paths():
            key   = str(path)
            since = self._last_seen.get(key, time.time() - 120)
            self._emit(_query_safari(path, since), key)

    def _emit(self, visits: list[tuple[str, str, float]], key: str):
        blocked_patterns = self._db.get_blocked_url_patterns()

        for url, title, ts in visits:
            if not url:
                continue

            host       = _extract_host(url)
            category   = _classify_url(url)
            is_blocked = self._check_blocked(url, host, blocked_patterns)

            event = {
                "type":     "url_blocked" if is_blocked else "url_visit",
                "url":      url,
                "title":    title,
                "app":      category,   # reuse app_name field for URL category
                "duration": 0,
                "ts":       int(ts),    # actual visit time — UTC Unix
            }
            self._db.insert_event(event)

            if is_blocked:
                log.info("Blocked URL visited: %s", host)
                self._shared["sync_trigger"].set()

            self._last_seen[key] = max(self._last_seen.get(key, 0), ts)

    @staticmethod
    def _check_blocked(url: str, host: str, patterns: list[str]) -> bool:
        url_lower  = url.lower()
        host_lower = host.lower()
        for pattern in patterns:
            p = pattern.lower().strip()
            if not p:
                continue
            # Exact host match, subdomain match, or substring match in URL
            if p == host_lower or host_lower.endswith("." + p) or p in url_lower:
                return True
        return False
