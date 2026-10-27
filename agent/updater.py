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
import plistlib
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

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
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        # macOS: trust ONLY a key shipped inside the app bundle (build_macos.sh
        # embeds it from UPDATE_PUBLIC_KEY_FILE). Never the user-writable data dir
        # or the repo's DEV key next to the sources.
        return [str(Path(sys.executable).parent.parent / "Resources")]
    dirs = [str(Path(__file__).resolve().parent), str(Config.BASE_DIR)]
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            dirs.append(str(Path(sys.executable).parent.parent / "Resources"))
        else:
            dirs.append(str(Path(sys.executable).parent / "_internal"))
    return dirs


def _report(status: str, target_version: str, *, auth: tuple | None = None, **kw) -> None:
    """Best-effort telemetry. Never raises.

    auth: optional (headers, endpoint) captured earlier — the macOS updater takes
    it BEFORE installing, because the pkg's preinstall deletes identity.json and
    the outcome (INSTALL_SUCCESS / INSTALL_FAILED) would otherwise go unreported.
    """
    headers, endpoint = auth if auth else (_auth_headers(), REPORT_ENDPOINT)
    if not headers:
        return
    try:
        httpx.post(endpoint, headers=headers,
                   json=uv.report_payload(status, target_version, **kw), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.debug("update report (%s) failed: %s", status, e)


_SAFE_VERSION = re.compile(r"\d{1,4}(\.\d{1,6}){1,3}")
_MAC_INSTALLER_TYPES = {"dmg"}


def _safe_macos_release(target: str, itype: str) -> bool:
    """macOS: the server-supplied version and installer_type become a path under
    the staging dir — accept only a plain dotted version and a known type, so a
    hostile response can never steer the download outside STAGING_DIR."""
    if not _SAFE_VERSION.fullmatch(target or "") or itype not in _MAC_INSTALLER_TYPES:
        return False
    staged = (STAGING_DIR / target / f"TimeTracker.{itype}").resolve()
    return staged.is_relative_to(STAGING_DIR.resolve())


def _report_outcome(status: str, target_version: str, auth: tuple, **kw) -> None:
    """macOS: report the install's final outcome reliably. The credentials were
    captured before the install, but the NEW agent re-enrolls right after the
    pkg's postinstall starts it and that rotates the device token — so on a 401
    (or no captured token) retry with the freshly enrolled identity for ~30 s."""
    headers, endpoint = auth
    body = uv.report_payload(status, target_version, **kw)
    deadline = time.time() + 30
    while True:
        if headers:
            try:
                r = httpx.post(endpoint, headers=headers, json=body, timeout=10)
                if r.status_code != 401:
                    return
            except Exception as e:  # noqa: BLE001
                log.debug("update report (%s) failed: %s", status, e)
                return
        if time.time() >= deadline:
            log.debug("update report (%s) dropped: no valid device token", status)
            return
        time.sleep(2)
        headers = _auth_headers()


def _download_headers(url: str) -> dict:
    """Request headers for the installer download.

    macOS: the device JWT is a credential for OUR API only. The download URL is
    normally a presigned S3 (or CDN) URL that is already authorized by its query
    string — S3/MinIO reject a request carrying a second auth mechanism (400
    "multiple authentication types"), and sending the JWT there would also leak it
    to a third-party host. So it is attached only when the URL points at the API.
    Windows keeps its existing (production) behaviour unchanged.
    """
    if sys.platform != "darwin":
        return _auth_headers() or {}
    api = urlsplit(identity.load().get("api_base") or Config.API_BASE)
    dst = urlsplit(url)
    # Same scheme AND host: never send the JWT to the API host over plain http
    # when the API itself is https.
    if api.netloc and (dst.scheme.lower(), dst.netloc.lower()) == (api.scheme.lower(), api.netloc.lower()):
        return _auth_headers() or {}
    return {}


def _manifest_matches_offer(manifest: dict, target: str, itype: str) -> str | None:
    """macOS: bind the unsigned top-level offer to the (signed) manifest so an old
    or foreign release can't be replayed under a new version label. Returns the
    reason it does not match, or None."""
    from datetime import datetime, timezone
    m = manifest or {}
    if m.get("version") != target:
        return "manifest version does not match the offered version"
    if m.get("platform") != "macos":
        return "manifest is not for macOS"
    if m.get("installer_type") != itype:
        return "manifest installer_type does not match"
    exp = m.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(str(exp).replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return "release has expired"
        except ValueError:
            return "unparseable expires_at"
    return None


# ── Worker: check → download → verify → stage → spawn updater ────────────────

class UpdateWorker:
    def __init__(self, shared: dict):
        self._shared = shared
        self._stop   = shared["stop_event"]
        self._http   = httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10))
        self._updater_proc = None   # macOS: the in-flight `--update` subprocess, if any

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
        if self._updater_proc is not None and self._updater_proc.poll() is None:
            # macOS keeps the agent running during an install: never start a second
            # updater (and a second concurrent install) while the first is running.
            log.info("Updater from a previous check is still running — skipping this check")
            return
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
        if sys.platform == "darwin":
            why_not = ("unsafe version/installer_type" if not _safe_macos_release(target, itype)
                       else _manifest_matches_offer(data.get("manifest"), target, itype))
            if why_not:
                log.warning("Rejecting update %r: %s", target[:40], why_not)
                _report("MANIFEST_VERIFY_FAILED", target[:40], error_code=why_not,
                        from_version=__version__)
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

        trust_key = uv.load_trust_key(_trust_key_dirs())
        ok, why = uv.verify_download(
            manifest=data.get("manifest") or {}, signature=data.get("signature"),
            file_path=str(staged), trust_key=trust_key,
            # macOS: once a publisher key ships in the bundle, a bad/missing
            # signature REFUSES the update (no never-brick fallback). Without a
            # bundled key behaviour is unchanged (sha256 only).
            strict=sys.platform == "darwin" and trust_key is not None,
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
                       agent_pid=os.getpid(),
                       sha256=((data.get("manifest") or {}).get("files") or [{}])[0].get("sha256"))
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
            headers = _download_headers(url)
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
            if sys.platform == "darwin":
                # Own session/process group: the pkg's preinstall boots the agent's
                # launchd job out, and launchd kills the whole job process group —
                # the updater (and the installer it is driving) must not be in it.
                self._updater_proc = subprocess.Popen(
                    [sys.executable, "--update"], close_fds=True, start_new_session=True,
                )
                # macOS: the pkg's preinstall stops this agent itself. Stopping first
                # (the old behaviour) left a Mac with NO tracking whenever the
                # install didn't complete (PRD-3738) — keep tracking until the
                # installer actually replaces us.
                log.info("Spawned updater subprocess for %s; agent keeps running until the installer replaces it", target)
                return
            subprocess.Popen([sys.executable, "--update"], close_fds=True)
            log.info("Spawned updater subprocess for %s; agent shutting down", target)
            self._stop.set()  # begin clean agent shutdown
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to launch updater subprocess: %s", e)
            _report("INSTALL_FAILED", target, error_code=f"spawn:{e}", from_version=__version__)


# ── Updater subprocess: run the native installer (symmetric macOS/Windows) ────
# Both platforms delegate to their OS installer on the downloaded artifact:
#   macOS   → installer -pkg -target CurrentUserHomeDirectory (per-user, no password;
#             the .pkg's pre/postinstall stop+replace+relaunch)
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

    # macOS: capture credentials + endpoint now — the pkg's preinstall deletes
    # identity.json, and the final outcome must still be reported.
    auth = None
    if plat == "darwin":
        ident = identity.load()
        base = (ident.get("api_base") or Config.API_BASE).rstrip("/")
        auth = (_auth_headers(), f"{base}/v1/updates/report")

    _report("INSTALL_STARTED", target, auth=auth, from_version=state.get("from_version"))
    if plat != "darwin":
        _wait_for_pid_exit(agent_pid, timeout=60)   # ensure the old agent is gone first

    try:
        if plat == "darwin":
            # Re-check the file right before installing: the staged copy lives in a
            # user-writable dir and the install happens later, in another process.
            expected = (state.get("sha256") or "").lower()
            if not expected or uv.sha256_file(staged).lower() != expected:
                raise RuntimeError("staged installer failed its pre-install sha256 check")
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
        if auth:
            _report_outcome("INSTALL_SUCCESS", target, auth, progress=100,
                            from_version=state.get("from_version"))
        else:
            _report("INSTALL_SUCCESS", target, progress=100, from_version=state.get("from_version"))
        log.info("Update to %s installed", target)
        return 0
    except Exception as e:  # noqa: BLE001
        # The native installer rolls itself back on failure (MSI is transactional;
        # a failed pkg install leaves the prior app in place). Clear state so the
        # next check re-offers the update — the same recovery model as Windows.
        log.error("Install failed: %s", e)
        if auth:
            _report_outcome("INSTALL_FAILED", target, auth, error_code=str(e)[:500],
                            from_version=state.get("from_version"))
        else:
            _report("INSTALL_FAILED", target, error_code=str(e), from_version=state.get("from_version"))
        uv.clear_state(STATE_FILE); _clear_lock()
        if plat == "darwin":
            # The pkg failed. The agent is normally still running (it is only
            # stopped by the install's preinstall); if the installer got far
            # enough to stop it, bring it back.
            _relaunch(plat)
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
    """The running bundle (…/TimeTracker.app/Contents/MacOS/timetracker →
    …/TimeTracker.app); the per-user install location otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parents[2]
    return Path.home() / "Applications" / "TimeTracker.app"


def _install_macos(dmg_path: str) -> None:
    """Run the .pkg inside the (sha256-verified) DMG as a PER-USER install.

    `installer -target CurrentUserHomeDirectory` installs into ~/Applications
    with no admin password and no root: the pkg's pre/postinstall scripts run as
    this user, stop the agent, replace the app, re-load the LaunchAgent and start
    the new version — silent, like the per-user Windows MSI.

    The mount point is read from `hdiutil attach -plist` (the old `-quiet` form
    printed nothing, so every install failed), into a private 0700 directory,
    read-only, and is always detached again.
    """
    mount_root = tempfile.mkdtemp(prefix="tt-update-")
    mnt = None
    try:
        attach = subprocess.run(
            ["hdiutil", "attach", dmg_path, "-plist", "-nobrowse", "-readonly",
             "-noautoopen", "-mountrandom", mount_root],
            capture_output=True, check=True, timeout=180,
        )
        entities = plistlib.loads(attach.stdout).get("system-entities", [])
        mnt = next((e["mount-point"] for e in entities if e.get("mount-point")), None)
        if not mnt:
            raise RuntimeError("DMG attached but no mount point was reported")
        pkg = next((os.path.join(mnt, e) for e in sorted(os.listdir(mnt))
                    if e.endswith(".pkg") and not e.startswith(".")), None)
        if not pkg:
            raise RuntimeError("no .pkg in DMG")
        r = subprocess.run(
            ["installer", "-pkg", pkg, "-target", "CurrentUserHomeDirectory"],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            raise RuntimeError(f"installer exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
        log.info("Installed pkg %s (per-user)", os.path.basename(pkg))
    finally:
        if mnt:
            if subprocess.run(["hdiutil", "detach", mnt, "-quiet"], check=False).returncode != 0:
                subprocess.run(["hdiutil", "detach", mnt, "-force", "-quiet"], check=False)
        # rmdir only (never a recursive delete): if a detach ever failed, the
        # volume must not be emptied — the empty dirs are just left behind.
        for d in ([mnt] if mnt else []) + [mount_root]:
            try:
                os.rmdir(d)
            except OSError:
                pass


def _install_windows(msi_path: str) -> None:
    # The MSI (WiX) terminates the running agent and atomically replaces files,
    # with its own transactional rollback if the install fails. Per-user scope
    # means no elevation prompt — this is the path that already works in prod.
    subprocess.run(["msiexec", "/i", msi_path, "/quiet", "/qn", "/norestart"], check=True)
    log.info("MSI install completed")


def _relaunch(plat: str) -> None:
    if plat == "darwin":
        # Prefer the LaunchAgent: a no-op when launchd already runs the agent (the
        # pkg's postinstall bootstraps it), and keeps restart-on-crash. `open` would
        # race that launch; it is only the fallback when the job isn't loaded.
        r = subprocess.run(
            ["launchctl", "kickstart", f"gui/{os.getuid()}/com.timetracker.agent"],
            capture_output=True, check=False,
        )
        if r.returncode != 0:
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
