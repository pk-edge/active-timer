"""
Persistent agent identity: device_id + JWT token + enrollment token.

Stored as a plain JSON file under the OS-appropriate per-user data dir.
On Windows this lives under %APPDATA%\\TimeTracker, on macOS under
~/Library/Application Support/TimeTracker, and on Linux under
~/.local/share/timetracker. Only the local user can read it (umask 0o077).

This is intentionally NOT in the system keychain: we want the agent to
run fully unattended after login with no keychain prompts.
"""

import json
import os
import stat
from pathlib import Path
from typing import Optional

from config import Config


_IDENTITY_FILE = Config.BASE_DIR / "identity.json"


def load() -> dict:
    """Return {device_id, jwt_token, company_token} or empty dict."""
    if not _IDENTITY_FILE.exists():
        return {}
    try:
        with _IDENTITY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def save(
    *,
    device_id: str,
    jwt_token: str,
    company_token: Optional[str] = None,
    api_base: Optional[str] = None,
    employee_email: Optional[str] = None,
) -> None:
    """Atomically write the identity file, owner-only readable."""
    existing       = load()
    company_token  = company_token  or existing.get("company_token")
    api_base       = api_base       or existing.get("api_base")
    employee_email = employee_email or existing.get("employee_email")

    data = {
        "device_id":      device_id,
        "jwt_token":      jwt_token,
        "company_token":  company_token,
        "api_base":       api_base,
        "employee_email": employee_email,
    }

    _IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _IDENTITY_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _IDENTITY_FILE)

    try:
        # 0o600 on POSIX; no-op on Windows but harmless
        os.chmod(_IDENTITY_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def clear() -> None:
    try:
        _IDENTITY_FILE.unlink()
    except FileNotFoundError:
        pass
