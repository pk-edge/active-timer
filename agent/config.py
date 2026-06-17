"""
Agent configuration — read-only static settings.

Per-device identity (device_id, jwt_token) is NOT here — it lives in
identity.py and is created/refreshed by enrollment.py on first run.
"""

import json
import os
import platform
import sys
from pathlib import Path
from typing import Optional


# ── Per-user data directory ──────────────────────────────────────────────────

if sys.platform == "win32":
    BASE_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TimeTracker"
elif sys.platform == "darwin":
    BASE_DIR = Path.home() / "Library" / "Application Support" / "TimeTracker"
else:
    BASE_DIR = Path.home() / ".local" / "share" / "timetracker"

BASE_DIR.mkdir(parents=True, exist_ok=True)


def _bundle_resources() -> Optional[Path]:
    """Return the bundle resources directory when running as a frozen app."""
    if not getattr(sys, "frozen", False):
        return None
    if sys.platform == "darwin":
        return Path(sys.executable).parent.parent / "Resources"
    return Path(sys.executable).parent / "_internal"


def _resolve_backends() -> list[dict]:
    """
    Return a list of backend configs in priority order.

    Each entry is a dict with keys:
      - url   : API base URL (e.g. https://tracker.example.com)
      - token : enrollment token for that backend (optional — may be None
                if a separate enrollment_token.txt supplies it)

    Resolution order (first match wins for the primary backend):
      1. TT_API_BASE env var  (single URL, token from TT_COMPANY_TOKEN)
      2. backends.json in bundle Resources / _internal  (frozen app)
      3. api_base.txt in bundle Resources / _internal  (legacy single-URL, frozen)
      4. backends.json next to config.py  (dev / non-frozen runs)
      5. backends.json in BASE_DIR  (runtime override / pushed config)
      6. Localhost fallback
    """
    def _parse(path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return [
                {"url": b["url"].rstrip("/"), "token": b.get("token") or None}
                for b in data if b.get("url")
            ]
        except Exception:
            return []

    # 1. Env var override
    if url := os.environ.get("TT_API_BASE", "").strip():
        token = os.environ.get("TT_COMPANY_TOKEN", "").strip() or None
        return [{"url": url, "token": token}]

    # 2 + 3. Frozen bundle resources (_internal\ on Windows, Resources/ on macOS)
    res = _bundle_resources()
    if res:
        bfile = res / "backends.json"
        if bfile.exists():
            if backends := _parse(bfile):
                return backends

        cfg = res / "api_base.txt"
        if cfg.exists():
            if url := cfg.read_text(encoding="utf-8-sig").strip():
                return [{"url": url, "token": None}]

    # 4. backends.json next to this file — dev / non-frozen runs
    bfile = Path(__file__).parent / "backends.json"
    if bfile.exists():
        if backends := _parse(bfile):
            return backends

    # 5. backends.json in user data dir — runtime override
    bfile = BASE_DIR / "backends.json"
    if bfile.exists():
        if backends := _parse(bfile):
            return backends

    return [{"url": "http://localhost:8000", "token": None}]


_BACKENDS: list[dict] = _resolve_backends()


def _resolve_api_base() -> str:
    return _BACKENDS[0]["url"]


class Config:
    BASE_DIR: Path = BASE_DIR

    # ── Server (resolved once at startup) ────────────────────────────────────
    # All backends in priority order; enrollment.py tries each in sequence.
    BACKENDS: list[dict] = _BACKENDS

    API_BASE: str = _resolve_api_base()
    ENROLL_ENDPOINT:        str = f"{API_BASE}/v1/auth/enroll"
    INGEST_ENDPOINT:        str = f"{API_BASE}/v1/ingest"
    UPLOAD_ENDPOINT:        str = f"{API_BASE}/v1/screenshots"
    UPDATE_CHECK_ENDPOINT:  str = f"{API_BASE}/v1/updates/check"
    POLICY_ENDPOINT:        str = f"{API_BASE}/v1/policy/blocked-urls"
    DEVICE_TODAY_ENDPOINT:  str = f"{API_BASE}/v1/home/device-today-active"

    # ── Auto-update ──────────────────────────────────────────────────────────
    AUTO_UPDATE_ENABLED: bool = os.environ.get("TT_AUTO_UPDATE", "1") != "0"
    UPDATE_CHECK_SECS:   int  = 6 * 3600   # poll for updates every 6 hours

    # ── Local storage ────────────────────────────────────────────────────────
    DB_PATH:        Path = BASE_DIR / "queue.db"
    SCREENSHOT_DIR: Path = BASE_DIR / "screenshots"
    LOG_PATH:       Path = BASE_DIR / "agent.log"

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Capture tuning ───────────────────────────────────────────────────────
    WINDOW_POLL_SECS:    int = 5
    SCREENSHOT_INTERVAL: int = 300
    IDLE_THRESHOLD_SECS: int = 300  # 5 minutes — stop timer if no input for 5 min
    URL_POLL_SECS:       int = 30   # poll browser history every 30s for sub-link fidelity

    # ── Policy / URL blocking ─────────────────────────────────────────────────
    POLICY_SYNC_SECS:    int = 300  # re-fetch blocked URL list every 5 minutes
    POLICY_PATH:         Path = BASE_DIR / "policy.json"

    # Media/meeting apps — never emit idle when these are the active window
    MEDIA_APPS: frozenset = frozenset({
        "zoom", "zoom.us", "us.zoom.videomeetings",
        "microsoft teams", "teams",
        "google meet",
        "webex", "cisco webex meetings",
        "facetime",
        "loom",
        "discord",
        "skype",
    })
    MEDIA_BROWSER_TITLES: frozenset = frozenset({
        "youtube", "netflix", "spotify", "amazon prime video",
        "disney+", "twitch", "hulu", "apple tv",
        "meet.google.com", "zoom.us",
    })

    # ── Sync tuning ──────────────────────────────────────────────────────────
    SYNC_INTERVAL_SECS:       int   = 5     # max wait before a sync (even with no trigger)
    SYNC_DEBOUNCE_SECS:       float = 0.5  # settle time after trigger fires (batches burst events)
    SYNC_BATCH_SIZE:          int   = 100
    SYNC_BACKOFF_BASE:        float = 2.0
    SYNC_BACKOFF_CAP:         int   = 300
    SCREENSHOT_SYNC_SECS:     int   = 60   # screenshots sync on their own longer interval

    # ── Platform info (sent during enrollment) ───────────────────────────────
    PLATFORM: str = sys.platform.lower()
    HOSTNAME: str = platform.node()
