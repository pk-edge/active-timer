"""
Prompt for administrator approval (UAC) before privileged actions.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger("win_admin")


def confirm_admin_elevation(title: str, message: str) -> bool:
    """
    Show the Windows UAC consent / credential dialog.
    Returns True only if the user successfully elevates (admin password on
    standard accounts).
    """
    if sys.platform != "win32":
        return True
    try:
        import win32api
        import win32con
        import win32gui

        # MB_ICONWARNING | MB_OKCANCEL — user must choose OK to proceed to UAC.
        if win32api.MessageBox(
            0,
            message,
            title,
            win32con.MB_ICONWARNING | win32con.MB_OKCANCEL | win32con.MB_TOPMOST,
        ) != win32con.IDOK:
            return False
    except Exception:
        pass

    # Launch elevated no-op; cancelled UAC returns <= 32.
    ret = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        "cmd.exe",
        "/c exit 0",
        None,
        0,
    )
    ok = ret > 32
    if not ok:
        log.info("Admin elevation declined or failed for: %s", title)
    return ok
