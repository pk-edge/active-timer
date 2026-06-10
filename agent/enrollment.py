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

    Supports multiple backends (Config.BACKENDS). Each backend is tried in
    order; the first that accepts enrollment wins and its URL is persisted in
    identity so all subsequent API calls use the same backend.

    max_attempts: 0 means retry forever (used at boot when network may be slow).
    shared: optional main-thread state dict; updates sync_status for the widget.
    """
    def _set_status(status: str) -> None:
        if shared is not None:
            shared["sync_status"] = status

    ident = identity.load()
    if ident.get("device_id") and ident.get("jwt_token"):
        # Restore the backend URL that was used at enrollment time so all
        # Config endpoints point to the correct server on subsequent runs.
        if stored_base := ident.get("api_base"):
            _apply_backend(stored_base)
        return ident

    # Build the list of (url, token) pairs to try, in order.
    candidates = _build_candidates(ident)

    if not candidates:
        _set_status("error")
        raise RuntimeError(
            "No enrollment token found. Set TT_COMPANY_TOKEN env var, "
            "write the token to %s/enrollment_token.txt, "
            "or embed backends.json in the installer bundle." % Config.BASE_DIR
        )

    attempt = 0
    backoff = 5
    _set_status("enrolling")

    while True:
        attempt += 1
        for backend_url, company_token in candidates:
            enroll_url = backend_url.rstrip("/") + "/v1/auth/enroll"
            body = {
                "enrollment_token": company_token,
                "hostname":         platform.node(),
                "platform":         sys.platform.lower(),
                "agent_version":    __version__,
            }
            try:
                log.info(
                    "Enrolling device (attempt %d) → %s",
                    attempt, enroll_url,
                )
                r = httpx.post(enroll_url, json=body, timeout=20)

                if r.status_code == 401:
                    log.warning(
                        "Enrollment rejected (401) at %s — token mismatch, trying next backend",
                        backend_url,
                    )
                    continue  # try the next backend

                r.raise_for_status()
                data = r.json()
                identity.save(
                    device_id     = data["device_id"],
                    jwt_token     = data["jwt_token"],
                    company_token = company_token,
                    api_base      = backend_url,
                )
                _apply_backend(backend_url)
                log.info(
                    "Enrollment successful: device_id=%s backend=%s",
                    data["device_id"], backend_url,
                )
                return identity.load()

            except httpx.HTTPStatusError as e:
                if e.response.status_code in (403,):
                    log.warning("Enrollment rejected (%d) at %s", e.response.status_code, backend_url)
                    continue
                log.warning("Enrollment HTTP error at %s: %s", backend_url, e)
            except Exception as e:
                log.warning("Enrollment failed at %s: %s", backend_url, e)

        # All backends failed this attempt — back off and retry
        _set_status("enrolling")
        if max_attempts and attempt >= max_attempts:
            _set_status("error")
            raise RuntimeError(
                "Enrollment failed after %d attempt(s). "
                "Check backend connectivity and enrollment token(s)." % attempt
            )
        time.sleep(min(backoff, 300))
        backoff = min(backoff * 2, 300)


def _build_candidates(ident: dict) -> list[tuple[str, str]]:
    """
    Return an ordered list of (api_base_url, enrollment_token) pairs to try.

    Priority:
      1. Config.BACKENDS entries that include an explicit token
      2. Env var TT_COMPANY_TOKEN combined with each backend URL
      3. Stored company_token from a previous enrollment
      4. enrollment_token.txt files on disk
    """
    env_token = os.environ.get("TT_COMPANY_TOKEN", "").strip() or None
    file_token = _read_company_token_file()
    stored_token = ident.get("company_token") or None

    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []

    def _add(url: str, token: str) -> None:
        key = (url, token)
        if key not in seen:
            seen.add(key)
            result.append(key)

    for backend in Config.BACKENDS:
        url = backend["url"]
        # Each backend may carry its own token (e.g. from backends.json)
        if backend.get("token"):
            _add(url, backend["token"])
        # Fall back to the global token sources
        if env_token:
            _add(url, env_token)
        if stored_token:
            _add(url, stored_token)
        if file_token:
            _add(url, file_token)

    return result


def _read_company_token_file() -> Optional[str]:
    """
    Look for enrollment_token.txt in order (first match wins):
      1. Data dir            — %APPDATA%\\TimeTracker\\ or ~/Library/.../TimeTracker/
      2. macOS bundle Resources — embedded by build_dmg.sh
      3. Windows _internal\\  — embedded by build_exe.ps1
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


def _apply_backend(url: str) -> None:
    """Update Config endpoints to point to the given backend URL."""
    base = url.rstrip("/")
    Config.API_BASE              = base
    Config.ENROLL_ENDPOINT       = f"{base}/v1/auth/enroll"
    Config.INGEST_ENDPOINT       = f"{base}/v1/ingest"
    Config.UPLOAD_ENDPOINT       = f"{base}/v1/screenshots"
    Config.UPDATE_CHECK_ENDPOINT = f"{base}/v1/updates/check"
    Config.POLICY_ENDPOINT       = f"{base}/v1/policy/blocked-urls"
    Config.DEVICE_TODAY_ENDPOINT = f"{base}/v1/home/device-today-active"
