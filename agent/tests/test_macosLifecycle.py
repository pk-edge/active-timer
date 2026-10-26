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
                   staged_path=str(staged), platform="darwin", agent_pid=os.getpid())
    waited, relaunched = [], []
    monkeypatch.setattr(updater, "_wait_for_pid_exit", lambda *a, **k: waited.append(a))
    monkeypatch.setattr(updater, "_report", lambda *a, **k: None)

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
