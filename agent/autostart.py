"""
Cross-platform auto-start on user login.

  Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
  macOS  : ~/Library/LaunchAgents/com.timetracker.agent.plist
  Linux  : ~/.config/autostart/timetracker-agent.desktop

The agent calls ``install()`` on every startup; it's idempotent and
costs nothing if already registered. This guarantees the timer UI
launches on every sign-in even if the user has not run the installer
yet (useful for portable / silent deployments).
"""

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("autostart")

APP_NAME    = "TimeTracker"
RUN_KEY     = APP_NAME
PLIST_LABEL = "com.timetracker.agent"


def install() -> bool:
    """Register the agent to launch at user login. Returns True on success."""
    exe = _agent_exe()
    if not exe:
        log.warning("autostart.install(): could not determine agent executable")
        return False
    try:
        if sys.platform == "win32":
            ok = _install_windows(exe)
            if ok:
                try:
                    from protection.win_guard import install_watchdog
                    install_watchdog()
                except Exception:
                    pass
            return ok
        if sys.platform == "darwin":
            return _install_macos(exe)
        return _install_linux(exe)
    except Exception as e:
        log.warning("autostart.install() failed: %s", e)
        return False


def uninstall() -> bool:
    try:
        if sys.platform == "win32":
            try:
                from protection.win_guard import uninstall_watchdog
                uninstall_watchdog()
            except Exception:
                pass
            return _uninstall_windows()
        if sys.platform == "darwin":
            return _uninstall_macos()
        return _uninstall_linux()
    except Exception as e:
        log.warning("autostart.uninstall() failed: %s", e)
        return False


# ── Resolve the binary to register ────────────────────────────────────────────

def _agent_exe() -> str:
    """
    When packaged with PyInstaller, sys.executable points at the .exe / .app
    binary directly. When running from source we register `python -m main`.
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    # Source / dev: run module
    return f'"{sys.executable}" "{Path(__file__).parent.resolve() / "main.py"}"'


# ── Windows ──────────────────────────────────────────────────────────────────

def _install_windows(exe: str) -> bool:
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    # /B keeps it windowless; quote to handle spaces in install path
    cmd = exe if exe.startswith('"') else f'"{exe}"'
    winreg.SetValueEx(key, RUN_KEY, 0, winreg.REG_SZ, cmd)
    winreg.CloseKey(key)
    log.info("Auto-start registered (Windows registry Run key)")
    return True


def _uninstall_windows() -> bool:
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, RUN_KEY)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass
    return True


# ── macOS ────────────────────────────────────────────────────────────────────

def _install_macos(exe: str) -> bool:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{PLIST_LABEL}.plist"

    # If exe contains a quoted python invocation we split into argv.
    argv = _split_argv(exe)
    program_args = "\n".join(f"      <string>{_xml(a)}</string>" for a in argv)

    log_path = Path.home() / "Library" / "Logs" / "TimeTracker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    contents = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>ProcessType</key>
    <string>Interactive</string>
  </dict>
</plist>
"""
    plist.write_text(contents)
    log.info("Auto-start registered (macOS LaunchAgent at %s)", plist)
    return True


def _uninstall_macos() -> bool:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
    try:
        plist.unlink()
    except FileNotFoundError:
        pass
    return True


# ── Linux ────────────────────────────────────────────────────────────────────

def _install_linux(exe: str) -> bool:
    autostart_dir = Path.home() / ".config" / "autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    desktop = autostart_dir / "timetracker-agent.desktop"
    desktop.write_text(
        f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exe}
X-GNOME-Autostart-enabled=true
NoDisplay=false
"""
    )
    log.info("Auto-start registered (Linux autostart .desktop file)")
    return True


def _uninstall_linux() -> bool:
    desktop = Path.home() / ".config" / "autostart" / "timetracker-agent.desktop"
    try:
        desktop.unlink()
    except FileNotFoundError:
        pass
    return True


# ── Helpers ──────────────────────────────────────────────────────────────────

def _split_argv(cmd: str) -> list[str]:
    """
    Minimal shlex-style split for our limited shapes:
      "/path/to/agent.exe"
      "/usr/bin/python" "/path/to/main.py"
    """
    import shlex
    return shlex.split(cmd, posix=(sys.platform != "win32"))


def _xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
