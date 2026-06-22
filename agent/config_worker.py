"""Polls the backend for runtime agent config and publishes it to `shared`.

Currently just the effective `headless` flag (per-device override or the
company-wide default). This is how an admin's "make agents headless/visible"
toggle reaches a running agent within ~60s, with no reinstall. The widget reads
shared["headless"] on its Qt-thread timer and hides/shows itself + the tray.
"""
import logging

import httpx

from config import Config
import identity

log = logging.getLogger("config_worker")


def _auth_headers() -> dict | None:
    token = identity.load().get("jwt_token", "")
    return {"Authorization": f"Bearer {token}"} if token else None


def fetch_headless() -> bool | None:
    """One-shot best-effort read of the effective headless flag (None on failure)."""
    headers = _auth_headers()
    if headers is None:
        return None
    try:
        r = httpx.get(Config.AGENT_CONFIG_ENDPOINT, headers=headers, timeout=8)
        if r.status_code == 200:
            return bool(r.json().get("headless", False))
    except Exception as e:  # noqa: BLE001
        log.debug("agent-config fetch failed: %s", e)
    return None


class ConfigWorker:
    def __init__(self, shared: dict):
        self._shared = shared
        self._stop   = shared["stop_event"]
        self._http   = httpx.Client(timeout=httpx.Timeout(connect=10, read=20, write=20, pool=10))

    def run(self) -> None:
        log.info("ConfigWorker started (interval=%ds)", Config.AGENT_CONFIG_SECS)
        try:
            while not self._stop.is_set():
                self._poll_once()
                if self._stop.wait(timeout=Config.AGENT_CONFIG_SECS):
                    break
        finally:
            self._http.close()
            log.info("ConfigWorker stopped")

    def _poll_once(self) -> None:
        headers = _auth_headers()
        if headers is None:
            return  # not enrolled yet — retry next cycle
        try:
            r = self._http.get(Config.AGENT_CONFIG_ENDPOINT, headers=headers)
            r.raise_for_status()
            headless = bool(r.json().get("headless", False))
        except Exception as e:  # noqa: BLE001
            # A failed poll must never crash the agent or flip visibility — keep
            # the last known value until the next successful fetch.
            log.debug("agent-config poll failed (keeping last value): %s", e)
            return
        if self._shared.get("headless") != headless:
            log.info("Agent visibility changed → headless=%s", headless)
        self._shared["headless"] = headless
