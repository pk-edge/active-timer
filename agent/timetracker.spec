import sys
from pathlib import Path

block_cipher = None

# Platform-specific hidden imports — only the relevant set is bundled per build
_win_imports = [
    "win32api", "win32con", "win32gui", "win32ts", "winreg",
] if sys.platform == "win32" else []

_mac_imports = [
    "AppKit", "Foundation", "Quartz", "objc",
] if sys.platform == "darwin" else []

a = Analysis(
    ["main.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[],
    hiddenimports=[
        # PySide6 plugins loaded at runtime
        "PySide6.QtWidgets",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "protection.win_guard",
        "protection.win_admin",
        "policy.policy_manager",
        # Cross-platform
        "psutil",
        "mss",
        "mss.tools",
        "pynput.keyboard",
        "pynput.mouse",
        "httpx",
        # Platform-specific
        *_win_imports,
        *_mac_imports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "test",
        "xmlrpc",
        "ftplib",
        "imaplib",
        "smtplib",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="timetracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no console window — runs silently in background
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # replace with "assets/icon.ico" (Windows) when available
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="timetracker",
)

# macOS: also produce a .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TimeTracker.app",
        icon=None,          # replace with "assets/icon.icns" when available
        bundle_identifier="com.timetracker.agent",
        info_plist={
            "CFBundleShortVersionString": "1.1.3",
            "CFBundleVersion":            "1.1.3",
            "NSHighResolutionCapable":    True,
            "NSAppleEventsUsageDescription":
                "TimeTracker reads the active window to track application usage.",
        },
    )
