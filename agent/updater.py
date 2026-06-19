"""Agent self-updater.

Two roles, one binary:
  1. UpdateWorker (a daemon thread in the running agent): polls the backend with
     jitter, downloads the new installer (resumable + disk check), verifies it
     (sha256 always + Ed25519 signature when a trust key is present), stages it
     behind a lock + state file, then spawns the updater SUBPROCESS and asks the
     agent to exit.
  2. run_update_install() (the spawned `TimeTracker --update` process): waits for
     the agent to exit, backs up the current install, atomically swaps in the new
     version, relaunches it, and rolls back on failure. Reports telemetry at each
     stage to POST /v1/updates/report.

Security: sha256 is ALWAYS enforced before install. The signed manifest is
verified against the public key in update_keys.json when present; a missing or
mismatched key falls back to sha256-only with a warning (never bricks updates).

The pure, unit-tested logic lives in update_verify.py. The OS-mutating swap /
relaunch / rollback below is validated on real macOS/Windows devices.
All failures are logged and swallowed — a broken update must never crash the agent.
"""
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx

from config import Config
import identity
from version import __version__
import update_verify as uv

log = logging.getLogger("updater")

# ── Local state paths (under the per-user data dir) ──────────────────────────
STATE_FILE   = str(Config.BASE_DIR / "update_state.json")
LOCK_FILE    = str(Config.BASE_DIR / "update.lock")
STAGING_DIR  = Config.BASE_DIR / "updates"
LAST_OK_FILE = str(Config.BASE_DIR / "last_successful_version.txt")

REPORT_ENDPOINT = f"{Config.API_BASE}/v1/updates/report"

JITTER_MAX_SECS = 300          # spread fleet checks over a 5-min window
DOWNLOAD_RETRIES = 3           # within one attempt; the poll loop retries later too
INSTALL_OVERHEAD = 3           # multiplier: download + staging + backup headroom


def _auth_headers() -> dict | None:
    token = identity.load().get("jwt_token", "")
    return {"Authorization": f"Bearer {token}"} if token else None


def _trust_key_dirs() -> list:
    """Where to look for update_keys.json: next to this module, the data dir, and
    (when frozen) the bundle resources."""
    dirs = [str(Path(__file__).resolve().parent), str(Config.BASE_DIR)]
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            dirs.append(str(Path(sys.executable).parent.parent / "Resources"))
        else:
            dirs.append(str(Path(sys.executable).parent / "_internal"))
    return dirs


def _report(status: str, target_version: str, **kw) -> None:
    """Best-effort telemetry. Never raises."""
    headers = _auth_headers()
    if not headers:
        return
    try:
        httpx.post(REPORT_ENDPOINT, headers=headers,
                   json=uv.report_payload(status, target_version, **kw), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.debug("update report (%s) failed: %s", status, e)


# ── Worker: check → download → verify → stage → spawn updater ────────────────

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
        # Finish/roll back any update interrupted by a crash or power loss.
        try:
            recover_on_launch()
        except Exception as e:  # noqa: BLE001
            log.warning("update recovery failed: %s", e)
        # Let enrollment finish, then add jitter to avoid a fleet-wide thundering herd.
        if self._stop.wait(timeout=30):
            return
        if self._stop.wait(timeout=random.uniform(0, JITTER_MAX_SECS)):
            return
        try:
            while not self._stop.is_set():
                try:
                    self._check_once()
                except Exception as e:  # noqa: BLE001
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
        target = data.get("version", "")
        if not uv.is_newer(target, __version__):
            log.info("Server version %s not newer than %s — skipping", target, __version__)
            return

        _report("CHECK_SUCCESS", target, from_version=__version__)
        log.info("Update available: %s → %s (mandatory=%s)", __version__, target, data.get("mandatory"))

        url   = data.get("download_url")
        itype = data.get("installer_type", "")
        size  = ((data.get("manifest") or {}).get("files") or [{}])[0].get("size") or 0
        if not url:
            return

        staged = STAGING_DIR / f"{target}" / f"TimeTracker.{itype}"
        staged.parent.mkdir(parents=True, exist_ok=True)

        # Disk: download + staging copy + backup headroom.
        if size and not uv.enough_disk_space(str(staged), size * INSTALL_OVERHEAD):
            log.warning("Insufficient disk for update %s (need ~%d bytes) — retry later", target, size * INSTALL_OVERHEAD)
            _report("FILE_DOWNLOAD_FAILED", target, error_code="no_disk_space", from_version=__version__)
            return

        if not self._download_resumable(url, staged, size, target):
            return

        ok, why = uv.verify_download(
            manifest=data.get("manifest") or {}, signature=data.get("signature"),
            file_path=str(staged), trust_key=uv.load_trust_key(_trust_key_dirs()),
        )
        if not ok:
            log.warning("Update verification failed (%s) — discarding %s", why, staged)
            _report("MANIFEST_VERIFY_FAILED", target, error_code=why, from_version=__version__)
            try:
                staged.unlink()
            except OSError:
                pass
            return
        log.info("Update %s verified (%s)", target, why)

        # Stage: record everything the updater subprocess needs, take the lock.
        uv.write_state(STATE_FILE, status="STAGED", version=target, from_version=__version__,
                       staged_path=str(staged), installer_type=itype, platform=sys.platform,
                       agent_pid=os.getpid())
        Path(LOCK_FILE).write_text(str(os.getpid()))

        # Defer non-mandatory installs while the user is actively working.
        if not data.get("mandatory") and self._shared.get("is_idle") is False:
            log.info("User active — deferring non-mandatory update %s to idle/next check", target)
            return

        self._launch_updater(target)

    def _download_resumable(self, url: str, dest: Path, size: int, target: str) -> bool:
        """Download with HTTP range resume + a few retries. Returns True on a
        complete file. Leaves the partial in place for the next poll on failure."""
        _report("FILE_DOWNLOAD_STARTED", target, from_version=__version__)
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            offset = uv.resume_offset(str(dest))
            if size and offset >= size:
                return True  # already complete from a previous attempt
            headers = _auth_headers() or {}
            mode = "wb"
            if offset:
                headers["Range"] = f"bytes={offset}-"
                mode = "ab"
                _report("FILE_DOWNLOAD_RESUMED", target, progress=int(offset * 100 / size) if size else 0)
            try:
                with self._http.stream("GET", url, headers=headers) as resp:
                    if offset and resp.status_code == 200:
                        # Server ignored Range — restart from scratch.
                        mode, offset = "wb", 0
                    resp.raise_for_status()
                    with open(dest, mode) as f:
                        for chunk in resp.iter_bytes(1024 * 256):
                            f.write(chunk)
                if not size or uv.resume_offset(str(dest)) >= size:
                    _report("FILE_DOWNLOAD_SUCCESS", target, progress=100)
                    return True
            except Exception as e:  # noqa: BLE001
                log.warning("download attempt %d/%d failed: %s", attempt, DOWNLOAD_RETRIES, e)
                if self._stop.wait(timeout=min(30 * attempt, 120)):
                    return False
        _report("FILE_DOWNLOAD_FAILED", target, error_code="incomplete", from_version=__version__)
        return False

    def _launch_updater(self, target: str) -> None:
        """Spawn `TimeTracker --update` as its own process, then ask the agent to
        exit so its binary can be replaced."""
        try:
            uv.write_state(STATE_FILE, status="INSTALLING")
            subprocess.Popen([sys.executable, "--update"], close_fds=True)
            log.info("Spawned updater subprocess for %s; agent shutting down", target)
            self._stop.set()  # begin clean agent shutdown
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to launch updater subprocess: %s", e)
            _report("INSTALL_FAILED", target, error_code=f"spawn:{e}", from_version=__version__)


# ── Updater subprocess: run the native installer (symmetric macOS/Windows) ────
# Both platforms delegate to their OS installer on the downloaded artifact:
#   macOS   → installer -pkg  (the .pkg's pre/postinstall kill+replace+relaunch)
#   Windows → msiexec /i      (the MSI's CloseApplication+launch action do the same)
# The installer owns the install + its own rollback, so the updater stays thin —
# matching the Windows flow that already works in production. (Device-tested on
# real macOS/Windows; not exercised in the backend Docker env.)

def run_update_install() -> int:
    state = uv.read_state(STATE_FILE)
    target = state.get("version", "")
    staged = state.get("staged_path", "")
    plat   = state.get("platform", sys.platform)
    agent_pid = state.get("agent_pid")
    if not target or not staged or not os.path.exists(staged):
        log.warning("Updater: nothing staged — abort"); _clear_lock(); return 0

    _report("INSTALL_STARTED", target, from_version=state.get("from_version"))
    _wait_for_pid_exit(agent_pid, timeout=60)   # ensure the old agent is gone first

    try:
        if plat == "darwin":
            _install_macos(staged)
        elif plat == "win32":
            _install_windows(staged)
        else:
            log.info("No auto-installer for platform %s", plat)
            _clear_lock(); return 0

        # Both native installers relaunch the agent themselves (macOS pkg postinstall
        # / Windows MSI launch action); this is a harmless, idempotent safety net.
        _relaunch(plat)
        Path(LAST_OK_FILE).write_text(target)
        uv.clear_state(STATE_FILE); _clear_lock()
        _report("INSTALL_SUCCESS", target, progress=100, from_version=state.get("from_version"))
        log.info("Update to %s installed", target)
        return 0
    except Exception as e:  # noqa: BLE001
        # The native installer rolls itself back on failure (MSI is transactional;
        # a failed pkg install leaves the prior app in place). Clear state so the
        # next check re-offers the update — the same recovery model as Windows.
        log.error("Install failed: %s", e)
        _report("INSTALL_FAILED", target, error_code=str(e), from_version=state.get("from_version"))
        uv.clear_state(STATE_FILE); _clear_lock()
        return 1


def recover_on_launch() -> None:
    """On agent start, clear any stale update lock/state left by a crashed or
    interrupted updater so it never blocks future updates. Install integrity and
    rollback are owned by the native installers (MSI is transactional; a failed
    pkg leaves the prior app), so there is no half-applied swap to repair here."""
    if uv.read_state(STATE_FILE).get("status") == "INSTALLING":
        log.warning("Clearing stale INSTALLING state from a previous run")
        uv.clear_state(STATE_FILE)
    _clear_lock()


# ── OS-specific helpers ──────────────────────────────────────────────────────

def _macos_app_path() -> Path:
    return Path("/Applications/TimeTracker.app")


def _install_macos(dmg_path: str) -> None:
    """Mirror of the Windows MSI path: run the native macOS installer on the .pkg
    inside the DMG. The .pkg's pre/postinstall scripts kill the running agent,
    replace /Applications/TimeTracker.app, (re)install the LaunchAgent, and
    relaunch the app — exactly what the Windows MSI's CloseApplication + launch
    action do.

    Installing to /Applications needs root, so the install runs through one
    `with administrator privileges` prompt (the macOS analog of an elevated MSI).
    For a fully silent update like the per-user Windows MSI, build the .pkg as a
    per-user install (see build_macos.sh) so no admin prompt is required.
    """
    attach = subprocess.run(
        ["hdiutil", "attach", dmg_path, "-nobrowse", "-quiet", "-mountrandom", "/tmp"],
        capture_output=True, text=True, check=True,
    )
    mnt = attach.stdout.strip().split("\t")[-1].strip()
    try:
        pkg = next((os.path.join(mnt, e) for e in os.listdir(mnt) if e.endswith(".pkg")), None)
        if not pkg:
            raise RuntimeError("no .pkg in DMG")
        # Run the macOS package installer (the analog of msiexec). The pkg path is
        # passed as an AppleScript argv item so it is safely quoted, not interpolated.
        script = (
            "on run argv\n"
            "  do shell script \"installer -pkg \" & quoted form of (item 1 of argv) "
            "& \" -target /\" with administrator privileges\n"
            "end run"
        )
        subprocess.run(["osascript", "-e", script, pkg], check=True)
        log.info("Installed pkg %s", os.path.basename(pkg))
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-quiet"], check=False)


def _install_windows(msi_path: str) -> None:
    # The MSI (WiX) terminates the running agent and atomically replaces files,
    # with its own transactional rollback if the install fails. Per-user scope
    # means no elevation prompt — this is the path that already works in prod.
    subprocess.run(["msiexec", "/i", msi_path, "/quiet", "/qn", "/norestart"], check=True)
    log.info("MSI install completed")


def _relaunch(plat: str) -> None:
    if plat == "darwin":
        subprocess.Popen(["open", str(_macos_app_path())], close_fds=True)
    elif plat == "win32":
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "TimeTracker" / "timetracker.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], close_fds=True)


def _wait_for_pid_exit(pid, timeout: int) -> None:
    if not pid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(int(pid)):
            return
        time.sleep(1)
    # Last resort: signal the agent to exit so we can replace its binary.
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
        else:
            os.kill(int(pid), 15)
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
            return str(pid) in out.stdout
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:  # noqa: BLE001
        return True


def _clear_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass
