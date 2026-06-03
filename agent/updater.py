"""UpdateWorker — polls the backend for a newer agent build and self-installs.

On launch (after a short delay) and every Config.UPDATE_CHECK_SECS, calls
GET /v1/updates/check with the device JWT. If the active server version differs
from the local version, downloads the installer and runs it:
  macOS   → mount the DMG, copy TimeTracker.app to /Applications, detach
  Windows → run the MSI silently (msiexec /i ... /quiet /qn /norestart)

All failures are logged and swallowed — a broken update must never crash the agent.
"""
import logging
import os
import subprocess
import sys
import tempfile
import time

import httpx

from config import Config
import identity
from version import __version__

log = logging.getLogger("updater")


def _auth_headers() -> dict | None:
    token = identity.load().get("jwt_token", "")
    return {"Authorization": f"Bearer {token}"} if token else None


def _parse_semver(v: str) -> tuple:
    parts = []
    for p in (v or "").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def _is_newer(remote: str, local: str) -> bool:
    return _parse_semver(remote) > _parse_semver(local)


class UpdateWorker:
    def __init__(self, shared: dict):
        self._shared = shared
        self._stop   = shared["stop_event"]
        self._http   = httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10))

    def run(self) -> None:
        if not Config.AUTO_UPDATE_ENABLED:
            log.info("Auto-update disabled")
            return
        log.info("UpdateWorker started (interval=%ds, current=%s)", Config.UPDATE_CHECK_SECS, __version__)
        # Small delay so enrollment completes first.
        if self._stop.wait(timeout=30):
            return
        try:
            while not self._stop.is_set():
                try:
                    self._check_once()
                except Exception as e:
                    log.warning("Update check failed: %s", e)
                if self._stop.wait(timeout=Config.UPDATE_CHECK_SECS):
                    break
        finally:
            self._http.close()
            log.info("UpdateWorker stopped")

    def _check_once(self) -> None:
        headers = _auth_headers()
        if headers is None:
            return
        r = self._http.get(Config.UPDATE_CHECK_ENDPOINT, headers=headers)
        r.raise_for_status()
        data = r.json()
        if not data.get("update_available"):
            return
        remote = data.get("version", "")
        if not _is_newer(remote, __version__):
            log.info("Server version %s not newer than local %s — skipping", remote, __version__)
            return

        log.info("Update available: %s → %s", __version__, remote)
        url = data.get("download_url")
        itype = data.get("installer_type", "")
        if not url:
            return
        path = self._download(url, itype)
        if not path:
            return
        if sys.platform == "darwin":
            self._install_macos(path)
        elif sys.platform == "win32":
            self._install_windows(path)
        else:
            log.info("No auto-installer for platform %s", sys.platform)

    def _download(self, url: str, itype: str) -> str | None:
        suffix = f".{itype}" if itype else ""
        fd, path = tempfile.mkstemp(prefix="tt-agent-update-", suffix=suffix)
        os.close(fd)
        try:
            with self._http.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in resp.iter_bytes(1024 * 256):
                        f.write(chunk)
            log.info("Downloaded update to %s", path)
            return path
        except Exception as e:
            log.warning("Download failed: %s", e)
            return None

    def _install_macos(self, dmg_path: str) -> None:
        mount = tempfile.mkdtemp(prefix="tt-dmg-")
        try:
            subprocess.run(["hdiutil", "attach", dmg_path, "-mountpoint", mount, "-nobrowse", "-quiet"], check=True)
            app = None
            for entry in os.listdir(mount):
                if entry.endswith(".app"):
                    app = os.path.join(mount, entry)
                    break
            if app:
                subprocess.run(["cp", "-R", app, "/Applications/"], check=True)
                log.info("Installed updated app from %s", app)
        except Exception as e:
            log.warning("macOS install failed: %s", e)
        finally:
            subprocess.run(["hdiutil", "detach", mount, "-quiet"], check=False)

    def _install_windows(self, msi_path: str) -> None:
        try:
            subprocess.run(["msiexec", "/i", msi_path, "/quiet", "/qn", "/norestart"], check=True)
            log.info("MSI install launched")
        except Exception as e:
            log.warning("Windows install failed: %s", e)
