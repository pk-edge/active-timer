"""Detect the first signed-in browser profile email (Chrome first, then Edge).

Reads the browser's 'Local State' JSON file — profile.info_cache.<dir>.user_name.
This is a plain file read of the profile email; no cookies, passwords or tokens
are touched. Every failure path returns None so enrollment falls back to an
unlinked device — detection must never crash the agent.
"""
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("email_detect")


def _local_state_paths() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        return [
            base / "Google" / "Chrome" / "Local State",
            base / "Microsoft Edge" / "Local State",
        ]
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        return [
            base / "Google" / "Chrome" / "User Data" / "Local State",
            base / "Microsoft" / "Edge" / "User Data" / "Local State",
        ]
    return [
        home / ".config" / "google-chrome" / "Local State",
        home / ".config" / "microsoft-edge" / "Local State",
    ]


def _emails_from_local_state(path: Path) -> list[str]:
    info = json.loads(path.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
    # "Default" profile first, remaining profiles in name order.
    ordered = sorted(info.items(), key=lambda kv: (kv[0] != "Default", kv[0]))
    return [
        str(v["user_name"]).strip().lower()
        for _, v in ordered
        if isinstance(v, dict) and "@" in str(v.get("user_name", ""))
    ]


def detect_first_signed_in_email() -> str | None:
    """First signed-in profile email across known browsers, or None."""
    for path in _local_state_paths():
        try:
            if not path.exists():
                continue
            emails = _emails_from_local_state(path)
            if emails:
                log.info("Detected signed-in email from %s", path)
                return emails[0]
        except Exception as e:
            log.debug("Could not read %s: %s", path, e)
    return None
