"""
Windows process protection for the TimeTracker agent.

- DACL: standard users cannot End Task in Task Manager (Access is denied).
  Administrators can still end the process (UAC / admin credentials).
- Watchdog: scheduled task restarts the agent if it is killed.
- Single instance: prevents duplicate agents.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("win_guard")

MUTEX_NAME = "Global\\TimeTrackerAgentMutex"
WATCHDOG_TASK = "TimeTrackerWatchdog"
WATCHDOG_INTERVAL_MIN = 2

_mutex_handle = None


def acquire_single_instance() -> bool:
    """Return False if another agent instance already holds the mutex."""
    if sys.platform != "win32":
        return True
    global _mutex_handle
    try:
        import win32api
        import win32event

        _mutex_handle = win32event.CreateMutex(None, True, MUTEX_NAME)
        if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            log.info("Another TimeTracker instance is already running — exiting")
            return False
        return True
    except Exception as e:
        log.warning("Single-instance mutex unavailable: %s", e)
        return True


def protect_process() -> bool:
    """
    Deny PROCESS_TERMINATE for normal users. Task Manager End Task fails
    unless the operator has administrator rights.
    """
    if sys.platform != "win32":
        return False
    try:
        import win32api
        import win32con
        import win32security

        handle = win32api.OpenProcess(win32con.WRITE_DAC, False, os.getpid())
        try:
            dacl = win32security.ACL()
            for sid_str in ("S-1-1-0", "S-1-5-32-545"):  # Everyone, Users
                dacl.AddAccessDeniedAce(
                    win32security.ACL_REVISION,
                    win32con.PROCESS_TERMINATE,
                    win32security.ConvertStringSidToSid(sid_str),
                )

            proc_token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
            )
            try:
                user_sid, _ = win32security.GetTokenInformation(
                    proc_token, win32security.TokenUser
                )
                dacl.AddAccessDeniedAce(
                    win32security.ACL_REVISION,
                    win32con.PROCESS_TERMINATE,
                    user_sid,
                )
            finally:
                win32api.CloseHandle(proc_token)

            for sid_str in ("S-1-5-32-544", "S-1-5-18"):  # Administrators, SYSTEM
                dacl.AddAccessAllowedAce(
                    win32security.ACL_REVISION,
                    win32con.PROCESS_ALL_ACCESS,
                    win32security.ConvertStringSidToSid(sid_str),
                )

            sd = win32security.SECURITY_DESCRIPTOR()
            sd.SetSecurityDescriptorDacl(1, dacl, 0)
            win32security.SetKernelObjectSecurity(
                handle, win32security.DACL_SECURITY_INFORMATION, sd
            )
            log.info("Process protection active — admin rights required to end task")
            return True
        finally:
            win32api.CloseHandle(handle)
    except Exception as e:
        log.warning("Process protection not applied: %s", e)
        return False


def install_watchdog() -> bool:
    """Register a per-user scheduled task that restarts the agent if killed."""
    if sys.platform != "win32":
        return False
    exe = _agent_executable()
    if not exe:
        return False
    tr = f'"{exe}" --watchdog'
    try:
        r = subprocess.run(
            [
                "schtasks", "/create", "/f",
                "/tn", WATCHDOG_TASK,
                "/sc", "minute",
                "/mo", str(WATCHDOG_INTERVAL_MIN),
                "/tr", tr,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0:
            log.info("Watchdog scheduled task registered (%s)", WATCHDOG_TASK)
            return True
        log.warning("Watchdog task registration failed: %s", (r.stderr or r.stdout).strip())
        return False
    except Exception as e:
        log.warning("Watchdog task registration failed: %s", e)
        return False


def uninstall_watchdog() -> None:
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["schtasks", "/delete", "/f", "/tn", WATCHDOG_TASK],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def is_agent_running() -> bool:
    """True if the main agent mutex is held (another instance is alive)."""
    if sys.platform != "win32":
        return False
    try:
        import win32api
        import win32event

        h = win32event.OpenMutex(win32event.SYNCHRONIZE, False, MUTEX_NAME)
        win32api.CloseHandle(h)
        return True
    except Exception:
        return False


def watchdog_tick() -> None:
    """Scheduled-task entry: start the agent when it is not running."""
    if is_agent_running():
        return
    exe = _agent_executable()
    if not exe:
        return
    log.info("Watchdog restarting TimeTracker")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([exe], creationflags=flags)


def _agent_executable() -> str | None:
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable
