"""
Device enrollment.

First-run flow:
  1. Read company_token from env (TT_COMPANY_TOKEN), a config file, or
     stored identity.
  2. POST to /v1/auth/enroll with hostname + platform.
  3. Persist returned device_id + jwt_token to identity.json.

Re-enrollment is automatic when /v1/auth/enroll is called again with the
same hostname — the backend upserts by (company_id, hostname).
"""

import logging
import os
import platform
import sys
import time
from typing import Optional

import httpx

from config import Config
import identity

log = logging.getLogger("enrollment")

from version import __version__


def ensure_enrolled(max_attempts: int = 0, shared: dict | None = None) -> dict:
    """
    Block until enrolled, then return the identity dict.

    max_attempts: 0 means retry forever (used at boot when network may be slow).
    shared: optional main-thread state dict; updates sync_status for the widget.
    """
    def _set_status(status: str) -> None:
        if shared is not None:
            shared["sync_status"] = status

    ident = identity.load()
    if ident.get("device_id") and ident.get("jwt_token"):
        return ident

    company_token = (
        os.environ.get("TT_COMPANY_TOKEN")
        or ident.get("company_token")
        or _read_company_token_file()
    )

    if not company_token:
        _set_status("error")
        raise RuntimeError(
            "No enrollment token found. Set TT_COMPANY_TOKEN env var "
            "or write the token to %s/enrollment_token.txt" % Config.BASE_DIR
        )

    body = {
        "enrollment_token": company_token,
        "hostname":         platform.node(),
        "platform":         sys.platform.lower(),
        "agent_version":    __version__,
    }

    attempt = 0
    backoff = 5
    _set_status("enrolling")
    while True:
        attempt += 1
        try:
            log.info("Enrolling device (attempt %d) → %s", attempt, Config.ENROLL_ENDPOINT)
            r = httpx.post(Config.ENROLL_ENDPOINT, json=body, timeout=20)
            if r.status_code == 401:
                log.error(
                    "Enrollment rejected (401): enrollment token does not match backend. "
                    "Use the token from seedAdmin and rebuild the installer, or update "
                    "%s/enrollment_token.txt",
                    Config.BASE_DIR,
                )
                _set_status("error")
                raise RuntimeError(
                    "Invalid enrollment token — check enrollment_token.txt matches your backend"
                )
            r.raise_for_status()
            data = r.json()
            identity.save(
                device_id     = data["device_id"],
                jwt_token     = data["jwt_token"],
                company_token = company_token,
            )
            log.info("Enrollment successful: device_id=%s", data["device_id"])
            return identity.load()

        except RuntimeError:
            raise
        except httpx.HTTPStatusError as e:
            log.warning("Enrollment failed: %s", e)
            if e.response.status_code in (401, 403):
                _set_status("error")
                raise RuntimeError(
                    f"Enrollment rejected ({e.response.status_code}) — check enrollment token"
                ) from e
            _set_status("enrolling")
            if max_attempts and attempt >= max_attempts:
                _set_status("error")
                raise
        except Exception as e:
            log.warning("Enrollment failed: %s", e)
            _set_status("enrolling")
            if max_attempts and attempt >= max_attempts:
                _set_status("error")
                raise
            time.sleep(min(backoff, 300))
            backoff = min(backoff * 2, 300)


def _read_company_token_file() -> Optional[str]:
    """
    Look for enrollment_token.txt in order (first match wins):
      1. Data dir            — %APPDATA%\\TimeTracker\\ or ~/Library/.../TimeTracker/
      2. macOS bundle Resources — embedded by build_dmg.sh
      3. Windows _internal\  — embedded by build_exe.ps1
    """
    from pathlib import Path as _Path

    candidates = [Config.BASE_DIR / "enrollment_token.txt"]

    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            # .../TimeTracker.app/Contents/MacOS/timetracker → up to Resources/
            candidates.append(
                _Path(sys.executable).parent.parent / "Resources" / "enrollment_token.txt"
            )
        elif sys.platform == "win32":
            # dist\timetracker\timetracker.exe → _internal\enrollment_token.txt
            candidates.append(
                _Path(sys.executable).parent / "_internal" / "enrollment_token.txt"
            )

    for p in candidates:
        if p.exists():
            try:
                val = p.read_text(encoding="utf-8-sig").strip()
                if val:
                    log.debug("Enrollment token found at %s", p)
                    return val
            except OSError:
                pass

    return None
