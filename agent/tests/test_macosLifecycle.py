"""PRD-3738 — the macOS agent must never become a live process that tracks nothing.

Covers:
  - SIGTERM really ends the process (end-to-end, real Qt loop, offscreen).
  - Single-instance lock, worker supervision, App Nap opt-out (macosRuntime).
  - Presence heartbeat when the queue is empty (SyncWorker, macOS only).
  - The updater no longer stops tracking before the macOS install happens.
  - The macOS lock detector survives a failing lock/unlock handler.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

import macosRuntime  # noqa: E402

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only behaviour")


# ── End-to-end: SIGTERM must end the process ─────────────────────────────────

def _agent_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "TT_API_BASE": "http://127.0.0.1:9",     # nothing listens: enrollment just retries
        "TT_COMPANY_TOKEN": "test-token",
        "TT_AUTO_UPDATE": "0",
        "QT_QPA_PLATFORM": "offscreen",
    })
    return env


def _wait_exit(proc, timeout):
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


@mac_only
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_terminates_the_whole_agent_process(tmp_path, sig):
    proc = subprocess.Popen([sys.executable, str(AGENT_DIR / "main.py")],
                            cwd=str(AGENT_DIR), env=_agent_env(tmp_path),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(6)                                   # workers + Qt loop running
        assert proc.poll() is None, "agent died on its own during startup"
        proc.send_signal(sig)
        code = _wait_exit(proc, 15)
        assert code is not None, "agent process lingered after the stop signal (zombie Qt loop)"
        assert code == 0
        log = (tmp_path / "Library/Application Support/TimeTracker/agent.log").read_text()
        assert "Qt event loop exited" in log
    finally:
        if proc.poll() is None:
            proc.kill()


@mac_only
def test_second_instance_exits_while_first_keeps_running(tmp_path):
    env = _agent_env(tmp_path)
    first = subprocess.Popen([sys.executable, str(AGENT_DIR / "main.py")], cwd=str(AGENT_DIR),
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)
        second = subprocess.run([sys.executable, str(AGENT_DIR / "main.py")], cwd=str(AGENT_DIR),
                                env=env, timeout=30, capture_output=True)
        assert second.returncode == 0
        assert first.poll() is None, "the first agent must be unaffected"
    finally:
        first.terminate()
        _wait_exit(first, 15) or first.kill()


# ── macosRuntime units ───────────────────────────────────────────────────────

@mac_only
def test_single_instance_lock_is_exclusive_and_released_on_exit(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import macosRuntime, pathlib; "
            "print(macosRuntime.acquire_single_instance(pathlib.Path(%r)))") % (str(AGENT_DIR), str(tmp_path))
    holder = subprocess.Popen([sys.executable, "-u", "-c", code + "; import time; time.sleep(5)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "True"
        other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        assert other.stdout.strip() == "False"
    finally:
        holder.kill()
        holder.wait()
    # The kernel released the flock when the holder died — no stale lock.
    again = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert again.stdout.strip() == "True"


def test_supervisor_restarts_a_crashed_worker_with_a_fresh_instance():
    stop = threading.Event()
    made = []

    class W:
        def __init__(self):
            made.append(self)

        def run(self):
            if len(made) < 3:
                raise RuntimeError("boom")
            stop.set()                                   # healthy third instance

    macosRuntime.run_supervised(W, stop, "W", backoff=0.01, max_backoff=0.02)
    assert len(made) == 3
    assert len({id(m) for m in made}) == 3               # never re-runs a closed instance


def test_supervisor_restarts_a_worker_that_returns_while_agent_runs():
    stop = threading.Event()
    runs = []

    class W:
        def run(self):
            runs.append(1)
            if len(runs) == 2:
                stop.set()

    macosRuntime.run_supervised(W, stop, "W", backoff=0.01)
    assert len(runs) == 2


def test_supervisor_does_not_restart_after_a_clean_stop():
    stop = threading.Event()
    runs = []

    class W:
        def run(self):
            runs.append(1)
            stop.set()

    macosRuntime.run_supervised(W, stop, "W", backoff=0.01)
    assert runs == [1]


@mac_only
def test_begin_activity_opts_out_of_app_nap():
    assert macosRuntime.begin_activity("pytest") is True
    assert macosRuntime._activity_token is not None


# ── SyncWorker heartbeat ─────────────────────────────────────────────────────

class _Resp:
    def __init__(self, code=202):
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Http:
    def __init__(self, code=202, exc=None):
        self.calls, self.code, self.exc = [], code, exc

    def post(self, url, json=None, headers=None):
        self.calls.append(json)
        if self.exc:
            raise self.exc
        return _Resp(self.code)

    def close(self):
        pass


class _DB:
    def __init__(self, batch=None):
        self.batch = batch or []
        self.synced = []

    def get_unsynced_events(self, _n):
        return self.batch

    def mark_events_synced(self, ids):
        self.synced += ids


def _worker(monkeypatch, db, http):
    import identity
    from sync import sync_worker as sw
    monkeypatch.setattr(identity, "load", lambda: {"jwt_token": "t"})
    shared = {"db": db, "pause_event": threading.Event(), "stop_event": threading.Event(),
              "sync_trigger": threading.Event(), "sync_status": "enrolling"}
    w = sw.SyncWorker(shared)
    w._http = http
    return w, shared


@mac_only
def test_idle_mac_with_empty_queue_sends_presence_heartbeat(monkeypatch):
    http = _Http()
    w, shared = _worker(monkeypatch, _DB(), http)
    w._sync_events()
    assert http.calls == [{"events": []}]
    assert shared["sync_status"] == "ok"
    w._sync_events()                                     # within HEARTBEAT_SECS → no spam
    assert len(http.calls) == 1


@mac_only
def test_heartbeat_is_skipped_right_after_a_real_sync(monkeypatch):
    ev = {"id": "e1", "event_type": "window_change", "app_name": "a", "window_title": "",
          "url": "", "duration": 5, "ts": int(time.time())}
    db = _DB([ev])
    http = _Http()
    w, _ = _worker(monkeypatch, db, http)
    w._sync_events()                                     # real batch = contact
    db.batch = []
    w._sync_events()
    assert len(http.calls) == 1 and http.calls[0]["events"]


@mac_only
def test_failed_heartbeat_feeds_backoff_and_status(monkeypatch):
    http = _Http(exc=OSError("network down"))
    w, shared = _worker(monkeypatch, _DB(), http)
    w._sync_events()
    assert w._fails == 1 and shared["sync_status"] == "syncing"


def test_no_heartbeat_off_macos(monkeypatch):
    from sync import sync_worker as sw
    monkeypatch.setattr(sw.sys, "platform", "win32")
    http = _Http()
    w, _ = _worker(monkeypatch, _DB(), http)
    w._sync_events()
    assert http.calls == []                              # Windows behaviour unchanged


# ── Updater: never stop tracking before the macOS install ────────────────────

@mac_only
def test_mac_update_handoff_keeps_the_agent_running(monkeypatch, tmp_path):
    import updater
    spawned = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "state.json"))
    stop = threading.Event()
    w = updater.UpdateWorker({"stop_event": stop})
    w._launch_updater("9.9.9")
    assert spawned, "updater subprocess must still be spawned"
    assert not stop.is_set(), "tracking must continue until the pkg actually replaces the agent"


@mac_only
def test_failed_mac_install_relaunches_and_does_not_wait_on_the_agent(monkeypatch, tmp_path):
    import updater
    import update_verify as uv
    staged = tmp_path / "TimeTracker.dmg"
    staged.write_bytes(b"x")
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(updater, "LOCK_FILE", str(tmp_path / "update.lock"))
    uv.write_state(updater.STATE_FILE, status="INSTALLING", version="9.9.9",
                   staged_path=str(staged), platform="darwin", agent_pid=os.getpid(),
                   sha256=uv.sha256_file(str(staged)))
    waited, relaunched = [], []
    monkeypatch.setattr(updater, "_wait_for_pid_exit", lambda *a, **k: waited.append(a))
    monkeypatch.setattr(updater, "_report", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_report_outcome", lambda *a, **k: None)

    def _fail(_p):
        raise RuntimeError("User canceled")
    monkeypatch.setattr(updater, "_install_macos", _fail)
    monkeypatch.setattr(updater, "_relaunch", lambda plat: relaunched.append(plat))
    assert updater.run_update_install() == 1
    assert waited == [], "must not SIGTERM a still-tracking agent before installing"
    assert relaunched == ["darwin"]


# ── Lock detector survives a failing handler ─────────────────────────────────

@mac_only
def test_macos_lock_detector_survives_handler_errors(monkeypatch):
    import Quartz
    from lock import lock_detector as ld

    states = iter([False, True, False, True])
    monkeypatch.setattr(Quartz, "CGSessionCopyCurrentDictionary",
                        lambda: {"CGSSessionScreenIsLocked": next(states, True)})
    calls = []

    def bad_lock():
        calls.append("lock")
        raise RuntimeError("database is locked")

    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: real_sleep(0.001))
    det = ld.LockDetector(on_lock=bad_lock, on_unlock=lambda: calls.append("unlock"))
    t = threading.Thread(target=det._run_macos, daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and calls.count("lock") < 2:
        real_sleep(0.01)
    assert calls[:3] == ["lock", "unlock", "lock"], calls   # kept going after the raise
    assert t.is_alive()


# ── Review follow-ups ────────────────────────────────────────────────────────

@mac_only
def test_unsupported_flock_runs_unguarded_instead_of_exiting(monkeypatch, tmp_path):
    import errno

    def no_flock(*_a):
        raise OSError(errno.ENOTSUP, "Operation not supported")   # e.g. SMB/NFS home
    monkeypatch.setattr(macosRuntime.fcntl, "flock", no_flock)
    monkeypatch.setattr(macosRuntime, "_lock_fh", None)
    assert macosRuntime.acquire_single_instance(tmp_path) is True


@mac_only
def test_updater_runs_in_its_own_session_and_is_never_started_twice(monkeypatch, tmp_path):
    import updater
    kwargs = []

    class _Proc:
        def poll(self):
            return None                                   # still waiting on the admin prompt

    def fake_popen(*a, **k):
        kwargs.append(k)
        return _Proc()
    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "state.json"))
    w = updater.UpdateWorker({"stop_event": threading.Event()})
    w._launch_updater("9.9.9")
    assert kwargs[0].get("start_new_session") is True    # survives launchd bootout of the agent job

    called = []
    monkeypatch.setattr(updater, "_auth_headers", lambda: called.append(1) or None)
    w._check_once()
    assert called == [], "a second check must not proceed while the first updater is alive"


@mac_only
def test_relaunch_prefers_launchd_kickstart(monkeypatch):
    import updater
    runs, opens = [], []

    class _R:
        returncode = 0
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **k: runs.append(cmd) or _R())
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **k: opens.append(cmd))
    updater._relaunch("darwin")
    assert runs and runs[0][:2] == ["launchctl", "kickstart"]
    assert opens == []


# ── Per-user silent update (no password) ─────────────────────────────────────

@mac_only
def test_download_never_sends_device_jwt_to_s3_or_cdn(monkeypatch):
    import identity
    import updater
    monkeypatch.setattr(identity, "load", lambda: {"jwt_token": "secret", "api_base": "https://api.tracker.onedge.co"})
    s3 = "https://bucket.s3.amazonaws.com/installers/x/TimeTracker.dmg?X-Amz-Signature=abc"
    assert updater._download_headers(s3) == {}
    assert updater._download_headers("https://cdn.tracker.onedge.co/installers/x.dmg") == {}
    api = "https://api.tracker.onedge.co/v1/updates/file/1"
    assert updater._download_headers(api) == {"Authorization": "Bearer secret"}


def test_download_headers_unchanged_off_macos(monkeypatch):
    import identity
    import updater
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(identity, "load", lambda: {"jwt_token": "secret"})
    assert updater._download_headers("https://bucket.s3.amazonaws.com/x?X-Amz-Signature=1") == \
        {"Authorization": "Bearer secret"}               # Windows production behaviour untouched


@mac_only
def test_install_macos_finds_the_pkg_installs_per_user_and_always_detaches(monkeypatch, tmp_path):
    import updater
    src = tmp_path / "src"
    src.mkdir()
    (src / "TimeTracker-9.9.9.pkg").write_bytes(b"not really a pkg")
    dmg = tmp_path / "TimeTracker.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", str(src), "-size", "5m",
                    "-format", "UDZO", "-ov", str(dmg)], check=True)

    real_run = subprocess.run
    calls = []

    class _R:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, *a, **k):
        if cmd[0] == "installer":                          # never really install in a test
            calls.append(cmd)
            assert os.path.exists(cmd[2]), "pkg must be on the mounted volume"
            return _R()
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    updater._install_macos(str(dmg))
    assert calls and calls[0][-2:] == ["-target", "CurrentUserHomeDirectory"]
    assert calls[0][2].endswith("TimeTracker-9.9.9.pkg")
    mounted = real_run(["hdiutil", "info"], capture_output=True, text=True).stdout
    assert str(tmp_path) not in mounted and "tt-update-" not in mounted, "DMG left mounted"


@mac_only
def test_failed_per_user_install_raises_with_installer_output(monkeypatch, tmp_path):
    import updater
    src = tmp_path / "src"
    src.mkdir()
    (src / "TimeTracker.pkg").write_bytes(b"x")
    dmg = tmp_path / "t.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", str(src), "-size", "5m",
                    "-format", "UDZO", "-ov", str(dmg)], check=True)
    real_run = subprocess.run

    class _R:
        returncode = 1
        stdout = ""
        stderr = "installer: Error - the package path specified was invalid"

    monkeypatch.setattr(updater.subprocess, "run",
                        lambda cmd, *a, **k: _R() if cmd[0] == "installer" else real_run(cmd, *a, **k))
    with pytest.raises(RuntimeError, match="installer exit 1"):
        updater._install_macos(str(dmg))
    assert "tt-update-" not in real_run(["hdiutil", "info"], capture_output=True, text=True).stdout


@mac_only
def test_install_outcome_is_reported_with_credentials_captured_before_install(monkeypatch, tmp_path):
    import identity
    import updater
    import update_verify as uv
    staged = tmp_path / "TimeTracker.dmg"
    staged.write_bytes(b"x")
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(updater, "LOCK_FILE", str(tmp_path / "update.lock"))
    monkeypatch.setattr(updater, "LAST_OK_FILE", str(tmp_path / "ok.txt"))
    uv.write_state(updater.STATE_FILE, status="INSTALLING", version="9.9.9",
                   staged_path=str(staged), platform="darwin", from_version="1.1.3",
                   sha256=uv.sha256_file(str(staged)))
    ident = {"jwt_token": "tok", "api_base": "https://api.example"}
    monkeypatch.setattr(identity, "load", lambda: dict(ident))
    sent = []

    class _Ok:
        status_code = 202
    monkeypatch.setattr(updater.httpx, "post", lambda url, headers=None, json=None, timeout=None:
                        sent.append((url, headers, json["status"])) or _Ok())

    def install(_p):
        ident.clear()                                    # preinstall deletes identity.json
    monkeypatch.setattr(updater, "_install_macos", install)
    monkeypatch.setattr(updater, "_relaunch", lambda plat: None)
    assert updater.run_update_install() == 0
    assert ("https://api.example/v1/updates/report", {"Authorization": "Bearer tok"}, "INSTALL_SUCCESS") in sent


# ── Security hardening (review follow-ups) ───────────────────────────────────

@mac_only
@pytest.mark.parametrize("version,itype", [
    ("9.0.0/../../../Library/LaunchAgents", "dmg"),
    ("../../x", "dmg"),
    ("9.9.9", "../../evil"),
    ("9.9.9", "sh"),
    ("", "dmg"),
])
def test_unsafe_release_fields_are_rejected(version, itype):
    import updater
    assert updater._safe_macos_release(version, itype) is False


@mac_only
def test_normal_release_fields_are_accepted():
    import updater
    assert updater._safe_macos_release("1.1.4", "dmg") is True
    assert updater._safe_macos_release("2.0.10.3", "dmg") is True


@mac_only
def test_tampered_staged_file_is_never_installed(monkeypatch, tmp_path):
    import updater
    import update_verify as uv
    staged = tmp_path / "TimeTracker.dmg"
    staged.write_bytes(b"original")
    monkeypatch.setattr(updater, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(updater, "LOCK_FILE", str(tmp_path / "update.lock"))
    uv.write_state(updater.STATE_FILE, status="INSTALLING", version="9.9.9", staged_path=str(staged),
                   platform="darwin", sha256=uv.sha256_file(str(staged)))
    staged.write_bytes(b"swapped after verification")
    installed = []
    monkeypatch.setattr(updater, "_install_macos", lambda p: installed.append(p))
    monkeypatch.setattr(updater, "_report", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_report_outcome", lambda *a, **k: None)
    monkeypatch.setattr(updater, "_relaunch", lambda plat: None)
    assert updater.run_update_install() == 1
    assert installed == []


@mac_only
def test_frozen_mac_trusts_only_the_bundled_key(monkeypatch, tmp_path):
    import updater
    app = tmp_path / "TimeTracker.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(app / "MacOS" / "timetracker"))
    assert updater._trust_key_dirs() == [str(app / "Resources")]


@mac_only
def test_offer_must_match_the_signed_manifest():
    import updater
    good = {"version": "1.1.4", "platform": "macos", "installer_type": "dmg", "expires_at": None}
    assert updater._manifest_matches_offer(good, "1.1.4", "dmg") is None
    assert updater._manifest_matches_offer({**good, "version": "1.0.0"}, "1.1.4", "dmg")    # replayed old build
    assert updater._manifest_matches_offer({**good, "platform": "windows"}, "1.1.4", "dmg")
    assert updater._manifest_matches_offer({**good, "installer_type": "msi"}, "1.1.4", "dmg")
    assert updater._manifest_matches_offer({**good, "expires_at": "2000-01-01T00:00:00+00:00"}, "1.1.4", "dmg")
    assert updater._manifest_matches_offer(None, "1.1.4", "dmg")


@mac_only
def test_jwt_never_goes_to_the_api_host_over_plain_http(monkeypatch):
    import identity
    import updater
    monkeypatch.setattr(identity, "load", lambda: {"jwt_token": "secret", "api_base": "https://api.tracker.onedge.co"})
    assert updater._download_headers("http://api.tracker.onedge.co/v1/x") == {}


@mac_only
def test_identity_file_is_owner_only(monkeypatch, tmp_path):
    import stat
    import identity
    monkeypatch.setattr(identity, "_IDENTITY_FILE", tmp_path / "identity.json")
    identity.save(device_id="d", jwt_token="t")
    assert stat.S_IMODE((tmp_path / "identity.json").stat().st_mode) == 0o600
    assert identity.load()["jwt_token"] == "t"
    identity.save(device_id="d", jwt_token="t2")            # overwrite path (stale tmp handled)
    assert identity.load()["jwt_token"] == "t2"


def test_presigned_signature_is_redacted_from_logs():
    import logging
    rec = logging.LogRecord("httpx", logging.INFO, "", 0, 'HTTP Request: %s %s "%s %d %s"',
                            ("GET", "https://s3.example/k.dmg?X-Amz-Signature=abc", "HTTP/1.1", 200, "OK"), None)
    macosRuntime._RedactQuery().filter(rec)
    assert "X-Amz-Signature" not in rec.getMessage() and "<redacted>" in rec.getMessage()


def test_redaction_covers_exception_text_from_other_loggers():
    import logging
    msg = "download attempt 1/3 failed: Client error '403' for url 'https://s3.example/k?X-Amz-Signature=abc&X-Amz-Credential=x'"
    rec = logging.LogRecord("updater", logging.WARNING, "", 0, "%s", (msg,), None)
    macosRuntime._RedactQuery().filter(rec)
    out = rec.getMessage()
    assert "X-Amz-Signature" not in out and "X-Amz-Credential" not in out and "https://s3.example/k?<redacted>" in out


def test_strict_mode_refuses_an_omitted_signature_when_a_key_is_bundled(tmp_path):
    import update_verify as uv
    f = tmp_path / "x.dmg"
    f.write_bytes(b"payload")
    m = {"files": [{"sha256": uv.sha256_file(str(f)), "size": 7}], "version": "9.9.9"}
    key = {"key_id": "v1", "public_key": "AAAA"}
    ok, why = uv.verify_download(manifest=m, signature=None, file_path=str(f), trust_key=key, strict=True)
    assert ok is False and "missing signature" in why
    ok, _ = uv.verify_download(manifest=m, signature=None, file_path=str(f), trust_key=None, strict=True)
    assert ok is True                                    # no key bundled: sha256-only, unchanged


@mac_only
def test_launchd_copy_waits_as_standby_and_takes_over(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import macosRuntime, pathlib; "
            "ok = macosRuntime.acquire_single_instance(pathlib.Path(%r), wait=%s); print(ok, flush=True)")
    holder = subprocess.Popen([sys.executable, "-u", "-c", code % (str(AGENT_DIR), str(tmp_path), False)
                               + "; import time; time.sleep(30)"], stdout=subprocess.PIPE, text=True)
    standby = None
    try:
        assert holder.stdout.readline().strip() == "True"
        standby = subprocess.Popen([sys.executable, "-u", "-c", code % (str(AGENT_DIR), str(tmp_path), True)],
                                   stdout=subprocess.PIPE, text=True)
        time.sleep(2)
        assert standby.poll() is None, "standby must block while the other copy runs"
        holder.kill()
        holder.wait()
        out, _ = standby.communicate(timeout=15)
        assert out.strip() == "True", "standby must take over once the lock is released"
    finally:
        for p in (holder, standby):
            if p and p.poll() is None:
                p.kill()


@mac_only
def test_system_quit_exits_non_zero_so_launchd_restarts(tmp_path):
    """macOS power-off/quit notification while the session continues → exit 75."""
    script = (
        "import sys, os; sys.path.insert(0, %r); os.environ['QT_QPA_PLATFORM']='offscreen';"
        "import threading; from PySide6.QtWidgets import QApplication; app = QApplication([]);"
        "from widget.floating_widget import FloatingWidget;"
        "shared = {'db': None, 'headless': True, 'stop_event': threading.Event(), 'pause_event': threading.Event()};"
        "w = FloatingWidget(shared); w._on_macos_power_off(); print(shared.get('exit_code'))"
    ) % str(AGENT_DIR)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "HOME": str(tmp_path)})
    assert r.stdout.strip().splitlines()[-1] == "75", r.stderr[-500:]


@mac_only
def test_outcome_report_retries_with_the_re_enrolled_token(monkeypatch):
    import updater
    calls = []

    class _R:
        def __init__(self, code):
            self.status_code = code

    def post(url, headers=None, json=None, timeout=None):
        calls.append(headers["Authorization"])
        return _R(401 if headers["Authorization"] == "Bearer old" else 202)
    monkeypatch.setattr(updater.httpx, "post", post)
    monkeypatch.setattr(updater, "_auth_headers", lambda: {"Authorization": "Bearer new"})
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    updater._report_outcome("INSTALL_SUCCESS", "9.9.9", ({"Authorization": "Bearer old"}, "https://api/x"))
    assert calls == ["Bearer old", "Bearer new"]
