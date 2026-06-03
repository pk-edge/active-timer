"""
Agent configuration — read-only static settings.

Per-device identity (device_id, jwt_token) is NOT here — it lives in
identity.py and is created/refreshed by enrollment.py on first run.
"""

import os
import platform
import sys
from pathlib import Path


# ── Per-user data directory ──────────────────────────────────────────────────

if sys.platform == "win32":
    BASE_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TimeTracker"
elif sys.platform == "darwin":
    BASE_DIR = Path.home() / "Library" / "Application Support" / "TimeTracker"
else:
    BASE_DIR = Path.home() / ".local" / "share" / "timetracker"

BASE_DIR.mkdir(parents=True, exist_ok=True)


def _resolve_api_base() -> str:
    # 1. Explicit env var always wins (dev/test override)
    if v := os.environ.get("TT_API_BASE", "").strip():
        return v

    # 2. api_base.txt embedded in the .app bundle Resources by build_dmg.sh / build_msi.ps1
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            bundle_res = Path(sys.executable).parent.parent / "Resources"
        else:
            bundle_res = Path(sys.executable).parent / "_internal"
        cfg = bundle_res / "api_base.txt"
        if cfg.exists():
            if v := cfg.read_text(encoding="utf-8-sig").strip():
                return v

    return "http://localhost:8000"


class Config:
    BASE_DIR: Path = BASE_DIR

    # ── Server (resolved once at startup) ────────────────────────────────────
    API_BASE: str = _resolve_api_base()
    ENROLL_ENDPOINT: str = f"{API_BASE}/v1/auth/enroll"
    INGEST_ENDPOINT: str = f"{API_BASE}/v1/ingest"
    UPLOAD_ENDPOINT: str = f"{API_BASE}/v1/screenshots"
    UPDATE_CHECK_ENDPOINT: str = f"{API_BASE}/v1/updates/check"

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
    IDLE_THRESHOLD_SECS: int = 60
    URL_POLL_SECS:       int = 120

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
