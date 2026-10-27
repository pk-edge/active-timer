import sys
from pathlib import Path

block_cipher = None

# Platform-specific hidden imports — only the relevant set is bundled per build
_win_imports = [
    "win32api", "win32con", "win32gui", "win32ts", "winreg",
] if sys.platform == "win32" else []

_mac_imports = [
    "AppKit", "Foundation", "Quartz", "objc", "macosRuntime",
] if sys.platform == "darwin" else []

# macOS ships ONE universal2 bundle that runs natively on Apple Silicon and
# Intel Macs. That needs every bundled binary to be fat: psutil and Pillow only
# publish single-arch macOS wheels and are not used on macOS at runtime
# (psutil serves the Windows/Linux window lookup; screenshots go through mss),
# so they are kept out of the macOS build.
_IS_MAC = sys.platform == "darwin"
_mac_excludes = ["psutil", "PIL"] if _IS_MAC else []

if _IS_MAC:
    sys.path.insert(0, str(Path(".").resolve()))
    from version import __version__ as _VERSION

a = Analysis(
    ["main.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[
        # backends.json is read by config.py from the bundle Resources (macOS)
        # or _internal (Windows) at runtime. It must be in the root of that dir.
        ("backends.json", "."),
    ],
    hiddenimports=[
        # PySide6 plugins loaded at runtime
        "PySide6.QtWidgets",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "protection.win_guard",
        "protection.win_admin",
        "policy.policy_manager",
        # Cross-platform
        *([] if _IS_MAC else ["psutil"]),
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
        *_mac_excludes,
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
    upx=not _IS_MAC,        # UPX breaks arm64 code signatures — never on macOS
    console=False,          # no console window — runs silently in background
    disable_windowed_traceback=False,
    target_arch="universal2" if _IS_MAC else None,
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
    upx=not _IS_MAC,
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
            "CFBundleShortVersionString": _VERSION,
            "CFBundleVersion":            _VERSION,
            "NSHighResolutionCapable":    True,
            # PySide6 6.8 (Qt 6.8) wheels require macOS 12 Monterey or later.
            "LSMinimumSystemVersion":     "12.0",
            # A windowless background agent must not be napped or auto-terminated
            # by macOS — that silently stops capture/sync (PRD-3738).
            "NSAppSleepDisabled":                True,
            "NSSupportsAutomaticTermination":    False,
            "NSSupportsSuddenTermination":       False,
            "NSAppleEventsUsageDescription":
                "TimeTracker reads the active window to track application usage.",
        },
    )
