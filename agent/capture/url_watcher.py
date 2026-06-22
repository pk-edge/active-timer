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


def _can_read(path: Path) -> bool:
    """True when the file can actually be opened (TCC may block despite mode bits)."""
    try:
        with open(path, "rb") as f:
            f.read(16)
        return True
    except OSError:
        return False


# ── Query helpers ─────────────────────────────────────────────────────────────

def _copy_locked_db(db_path: Path) -> str | None:
    """Copy a browser's live DB (plus WAL/journal sidecars) to a temp file.

    Firefox and Safari run their history DBs in WAL mode — the most recent
    visits live in the `-wal` sidecar, so copying only the main file silently
    misses everything since the last checkpoint.
    """
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        shutil.copy2(db_path, tmp.name)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                try:
                    shutil.copy2(sidecar, tmp.name + suffix)
                except Exception:
                    pass
        return tmp.name
    except Exception as e:
        log.debug("Could not copy %s: %s", db_path, e)
        return None


def _remove_db_copy(tmp: str) -> None:
    import os
    for path in (tmp, tmp + "-wal", tmp + "-shm", tmp + "-journal"):
        try:
            os.remove(path)
        except OSError:
            pass


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
        log.warning("Chromium history read error (%s): %s", db_path, e)
    finally:
        _remove_db_copy(tmp)
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
        log.warning("Firefox history read error (%s): %s", db_path, e)
    finally:
        _remove_db_copy(tmp)
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
        log.warning("Safari history read error (%s): %s", db_path, e)
    finally:
        _remove_db_copy(tmp)
    return results


# ── UrlWatcher ────────────────────────────────────────────────────────────────

class UrlWatcher:
    def __init__(self, shared: dict):
        self._db         = shared["db"]
        self._shared     = shared
        self._pause      = shared["pause_event"]
        self._stop       = shared["stop_event"]
        self._last_seen: dict[str, float] = {}
        self._logged_discovery = False
        self._warned_safari    = False

    def run(self):
        log.info("UrlWatcher started (poll=%ds)", Config.URL_POLL_SECS)
        while not self._stop.is_set():
            if not self._pause.is_set():
                # One bad poll (locked DB, schema change, disk hiccup) must
                # never kill the thread — URL capture would be dead until the
                # next agent restart.
                try:
                    self._poll()
                except Exception as e:
                    log.warning("URL poll failed — will retry next cycle: %s", e)
            for _ in range(Config.URL_POLL_SECS):
                if self._stop.is_set():
                    break
                time.sleep(1)
        log.info("UrlWatcher stopped")

    def _poll(self):
        chromium = _chromium_paths()
        firefox  = _firefox_paths()
        safari   = _safari_paths()

        if not self._logged_discovery:
            self._logged_discovery = True
            log.info("Browser history DBs found: chromium=%d firefox=%d safari=%d",
                     len(chromium), len(firefox), len(safari))
            for p in chromium + firefox + safari:
                log.info("  watching: %s", p)
            if not (chromium or firefox or safari):
                log.warning("No browser history DBs found — URL capture inactive")

        for path in chromium:
            key   = str(path)
            since = self._last_seen.get(key, time.time() - 120)
            self._emit(_query_chromium(path, since), key)

        for path in firefox:
            key   = str(path)
            since = self._last_seen.get(key, time.time() - 120)
            self._emit(_query_firefox(path, since), key)

        for path in safari:
            key   = str(path)
            since = self._last_seen.get(key, time.time() - 120)
            visits = _query_safari(path, since)
            if not self._warned_safari and not _can_read(path):
                self._warned_safari = True
                log.warning(
                    "Safari history exists at %s but is unreadable — Safari URL capture "
                    "is disabled. To enable it: System Settings → Privacy & Security → "
                    "Full Disk Access → add TimeTracker. Window activity (app=Safari, "
                    "duration) is still tracked without this permission.", path
                )
            self._emit(visits, key)

    def _emit(self, visits: list[tuple[str, str, float]], key: str):
        if not visits:
            return
        blocked_patterns = self._db.get_blocked_url_patterns()
        emitted = 0

        for url, title, ts in visits:
            self._last_seen[key] = max(self._last_seen.get(key, 0), ts)
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
            try:
                self._db.insert_event(event)
                emitted += 1
            except Exception as e:
                log.warning("Could not queue url event: %s", e)

            if is_blocked:
                log.info("Blocked URL visited: %s", host)

        if emitted:
            log.debug("Queued %d url events from %s", emitted, key)
            self._shared["sync_trigger"].set()

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
