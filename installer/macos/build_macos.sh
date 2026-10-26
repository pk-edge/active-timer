#!/usr/bin/env bash
# Build TimeTracker macOS installer DMG
#
# Flow for the end-user:
#   1. Open the DMG
#   2. Double-click TimeTracker-<version>.pkg
#   3. Click through the installer wizard → Install
#   4. TimeTracker is copied to /Applications and launches immediately
#
# Usage:
#   ./build_macos.sh                       — uses enrollment_token.txt + backends.json if present
#   ./build_macos.sh <token> [<api_url>]   — embeds the given token and optional API URL
#
# Prerequisites: Python venv with pyinstaller, pkgbuild + hdiutil (built-in macOS)
#
# Output: dist/TimeTracker-<version>.dmg  (contains a .pkg installer inside)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../../agent" && pwd)"
cd "$AGENT_DIR"

VENV="$AGENT_DIR/.venv"

if [ ! -x "$VENV/bin/pyinstaller" ]; then
    echo "ERROR: PyInstaller not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

# One bundle for every Mac needs a universal2 (arm64 + x86_64) interpreter —
# the python.org installer build is; Homebrew/pyenv builds are single-arch.
PY_ARCHS=$(lipo -archs "$("$VENV/bin/python" -c 'import sys; print(sys.executable)')" 2>/dev/null || true)
if [[ " $PY_ARCHS " != *" arm64 "* || " $PY_ARCHS " != *" x86_64 "* ]]; then
    echo "ERROR: $VENV uses a single-arch Python ($PY_ARCHS). Recreate it from the python.org"
    echo "       universal2 build: /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv .venv"
    exit 1
fi

# ── Version ───────────────────────────────────────────────────────────────────
VERSION=$("$VENV/bin/python" -c "from version import __version__; print(__version__)")
echo "Building TimeTracker v${VERSION} …"

ARG_TOKEN="${1:-}"
ARG_URL="${2:-}"

# ── Temp dirs (all cleaned up on exit) ───────────────────────────────────────
SCRIPTS_DIR=$(mktemp -d)
ROOT_DIR=$(mktemp -d)
STAGING=$(mktemp -d)
STAGING_PKG_DIR=$(mktemp -d)
trap 'rm -rf "$SCRIPTS_DIR" "$ROOT_DIR" "$STAGING" "$STAGING_PKG_DIR"' EXIT

# Oldest macOS the bundle supports (PySide6 6.8 / Qt 6.8 → macOS 12 Monterey).
# Keep in sync with LSMinimumSystemVersion in agent/timetracker.spec.
MIN_MACOS="12.0"

# ── 0. universal2 dependencies ────────────────────────────────────────────────
# The bundle must run natively on Apple Silicon and Intel Macs, so every native
# extension has to be a fat (arm64 + x86_64) binary. Some packages only publish
# per-arch macOS wheels: fetch the official wheel for each architecture and fuse
# the extension modules with lipo. Independent of the build machine's SDK (recent
# SDKs can no longer link x86_64 at all).
PYVER=$("$VENV/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
SITE=$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["platlib"])')
FAT_TMP=$(mktemp -d)
make_fat() {
    local dist="$1" ver
    ver=$("$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('$dist'))")
    for arch in arm64:macosx_11_0_arm64 x86_64:macosx_10_13_x86_64; do
        "$VENV/bin/pip" download -q --only-binary=:all: --no-deps \
            --platform "${arch#*:}" --python-version "$PYVER" \
            -d "$FAT_TMP/$dist-${arch%%:*}" "$dist==$ver"
        (cd "$FAT_TMP/$dist-${arch%%:*}" && unzip -q -o ./*.whl)
    done
    (cd "$FAT_TMP/$dist-arm64" && find . -name '*.so') | while read -r so; do
        lipo -create "$FAT_TMP/$dist-arm64/$so" "$FAT_TMP/$dist-x86_64/$so" -output "$SITE/$so"
        echo "  fused $so → $(lipo -archs "$SITE/$so")"
    done
}
make_fat cffi
rm -rf "$FAT_TMP"

# ── 1. PyInstaller ────────────────────────────────────────────────────────────
"$VENV/bin/pyinstaller" timetracker.spec --noconfirm
echo "PyInstaller complete."

# ── 1b. Every Mac: the bundle must run natively on Apple Silicon AND Intel ────
APP_BIN="$AGENT_DIR/dist/TimeTracker.app/Contents/MacOS/timetracker"
is_fat() {
    local archs=" $(lipo -archs "$1" 2>/dev/null) "
    [[ $archs == *" arm64 "* && $archs == *" x86_64 "* ]]
}
NON_FAT=$(find "$AGENT_DIR/dist/TimeTracker.app" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) \
    | while read -r f; do
        lipo -archs "$f" >/dev/null 2>&1 || continue      # not Mach-O
        is_fat "$f" || echo "$(lipo -archs "$f")  ${f#"$AGENT_DIR/dist/"}"
    done)
if [ -n "$NON_FAT" ]; then
    echo "ERROR: these binaries are not universal2 (would not run on every Mac):"
    echo "$NON_FAT"
    exit 1
fi
echo "Architectures: $(lipo -archs "$APP_BIN") (all bundled Mach-O files are universal2)."

# ── 2. Embed config files into the app bundle ─────────────────────────────────
RESOURCES="$AGENT_DIR/dist/TimeTracker.app/Contents/Resources"

# backends.json is the single source of truth for URL + token.
# If args were passed, write them into backends.json format so everything
# stays in one place and the agent's re-enrollment detection works correctly.
if [ -n "$ARG_TOKEN" ] && [ -n "$ARG_URL" ]; then
    printf '[{"url":"%s","token":"%s"}]\n' "$ARG_URL" "$ARG_TOKEN" > "$RESOURCES/backends.json"
    echo "Embedded backends.json from arguments (url=$ARG_URL)."
elif [ -n "$ARG_TOKEN" ]; then
    # Token only — inject into existing backends.json if present, otherwise keep
    # the token as a standalone file (no URL override).
    if [ -f "$AGENT_DIR/backends.json" ]; then
        cp "$AGENT_DIR/backends.json" "$RESOURCES/"
        echo "Embedded backends.json (token arg ignored — backends.json has its own token)."
    else
        printf '%s' "$ARG_TOKEN" > "$RESOURCES/enrollment_token.txt"
        echo "Embedded enrollment_token.txt (no backends.json found)."
    fi
elif [ -f "$AGENT_DIR/backends.json" ]; then
    cp "$AGENT_DIR/backends.json" "$RESOURCES/"
    echo "Embedded backends.json."
fi

# Re-seal the bundle: PyInstaller ad-hoc signed it before backends.json was
# written into Resources, which invalidates the seal ("a sealed resource is
# missing or invalid") — a broken seal makes Gatekeeper/TCC treat the app as
# tampered. (Ad-hoc identities still change per build, so permission grants
# survive an update only once the app is signed with a stable Developer ID.)
codesign --force --deep --sign - "$AGENT_DIR/dist/TimeTracker.app"
codesign --verify --deep --strict "$AGENT_DIR/dist/TimeTracker.app"
echo "Bundle re-signed (ad-hoc) and verified."

# ── 3. Installer scripts ──────────────────────────────────────────────────────

# preinstall: stop the running agent (for real) + clear stale localhost identity
cat > "$SCRIPTS_DIR/preinstall" << 'EOF'
#!/bin/bash
# 1. Unload the LaunchAgent of every logged-in GUI user first, so launchd cannot
#    respawn the old binary while the payload is being replaced (postinstall
#    loads it again for each of them).
for USER_UID in $(ps -axo uid=,comm= | awk '$2 ~ /loginwindow$/ && $1 != 0 {print $1}' | sort -u); do
    launchctl bootout "gui/$USER_UID/com.timetracker.agent" 2>/dev/null || true
done

# 2. Stop every running agent, but NOT the self-updater (`timetracker --update`)
#    that may be the process running this very installer. Wait for a real exit
#    and force-kill a straggler — an old agent that ignored SIGTERM used to
#    survive here and was merely re-activated by postinstall's `open -a`,
#    leaving a Mac with a live process that tracked nothing (PRD-3738).
agent_pids() {
    for PID in $(pgrep -x timetracker 2>/dev/null); do
        case " $(ps -o args= -p "$PID" 2>/dev/null) " in
            *" --update "*) ;;
            *) echo "$PID" ;;
        esac
    done
}
PIDS=$(agent_pids)
if [ -n "$PIDS" ]; then
    kill -TERM $PIDS 2>/dev/null || true
    for _ in $(seq 1 20); do
        [ -z "$(agent_pids)" ] && break
        sleep 0.5
    done
    PIDS=$(agent_pids)
    [ -n "$PIDS" ] && kill -KILL $PIDS 2>/dev/null || true
fi

# Clear the stored identity on every install so the agent re-enrolls fresh
# against the backend configured in backends.json. This ensures a URL or token
# change in backends.json always takes effect without manual cleanup.
for HOME_DIR in /Users/*/; do
    IDENTITY="$HOME_DIR/Library/Application Support/TimeTracker/identity.json"
    if [ -f "$IDENTITY" ]; then
        rm -f "$IDENTITY"
        echo "Cleared identity at $IDENTITY (will re-enroll on next launch)"
    fi
done

exit 0
EOF

# postinstall: register the LaunchAgent with launchd and let launchd start it,
# for EVERY logged-in GUI user (preinstall stopped all of their agents).
cat > "$SCRIPTS_DIR/postinstall" << 'EOF'
#!/bin/bash
# Installer runs as root. A logged-in GUI user owns a loginwindow process; the
# login screen's loginwindow is root's (uid 0) and is skipped.
GUI_UIDS=$(ps -axo uid=,comm= | awk '$2 ~ /loginwindow$/ && $1 != 0 {print $1}' | sort -u)

setup_user() {
    local USER_UID="$1" USER_NAME USER_HOME PLIST_DIR PLIST LOG_FILE D
    USER_NAME=$(id -un "$USER_UID" 2>/dev/null) || return 0
    # dscacheutil keeps spaces in the path ("/Users/John Smith").
    USER_HOME=$(dscacheutil -q user -a name "$USER_NAME" 2>/dev/null | sed -n 's/^dir: //p' | head -1)
    [ -z "$USER_HOME" ] && USER_HOME="/Users/$USER_NAME"
    [ -d "$USER_HOME" ] || return 0

    # LaunchAgent for auto-start on login (idempotent). Same content the agent's
    # autostart.install() writes, so its per-launch rewrite is a no-op.
    PLIST_DIR="$USER_HOME/Library/LaunchAgents"
    PLIST="$PLIST_DIR/com.timetracker.agent.plist"
    LOG_FILE="$USER_HOME/Library/Logs/TimeTracker.log"

    # SECURITY: this script runs as root, but $USER_HOME is user-controlled — a
    # symlink planted at the plist path (or its directories) would make a root
    # write/chown hit an arbitrary file. Every per-user file operation therefore
    # runs AS THE USER (as_user), so symlinks can only reach what they could
    # already write. Root only renders the content into its own private temp file.
    as_user() { sudo -u "$USER_NAME" "$@"; }
    as_user mkdir -p "$PLIST_DIR" "$USER_HOME/Library/Logs" || return 0

    PLIST_TMP=$(mktemp)
    cat > "$PLIST_TMP" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.timetracker.agent</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Applications/TimeTracker.app/Contents/MacOS/timetracker</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
    <key>ProcessType</key>
    <string>Interactive</string>
  </dict>
</plist>
PLIST
    # stdin is opened by root, the write happens as the user.
    as_user tee "$PLIST" < "$PLIST_TMP" > /dev/null || { rm -f "$PLIST_TMP"; return 0; }
    rm -f "$PLIST_TMP"
    as_user chmod 644 "$PLIST"

    # Start the agent UNDER launchd (RunAtLoad), not via `open -a`: launchd then
    # restarts it if it ever crashes, and there is exactly one launch path.
    # Bootout first so a re-install replaces a loaded job; a job still tearing
    # down makes bootstrap fail with EIO (5), so retry briefly.
    launchctl bootout "gui/$USER_UID/com.timetracker.agent" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        launchctl bootstrap "gui/$USER_UID" "$PLIST" 2>/dev/null && return 0
        sleep 1
    done
    # No launchd GUI domain reachable (rare) — plain launch; the LaunchAgent
    # takes over from the next login.
    launchctl asuser "$USER_UID" open -a /Applications/TimeTracker.app || true
}

for U in $GUI_UIDS; do
    setup_user "$U"
done

exit 0
EOF

chmod +x "$SCRIPTS_DIR/preinstall" "$SCRIPTS_DIR/postinstall"

# ── 4. Build the .pkg installer ───────────────────────────────────────────────
# pkgbuild --root copies everything from ROOT_DIR into --install-location.
# So we place TimeTracker.app inside ROOT_DIR → it lands at /Applications/TimeTracker.app.
cp -R "$AGENT_DIR/dist/TimeTracker.app" "$ROOT_DIR/"

PKG_PATH="$AGENT_DIR/dist/TimeTracker-${VERSION}.pkg"
COMPONENT_PKG="$STAGING_PKG_DIR/TimeTracker-component.pkg"

# Non-relocatable: otherwise Installer "updates" any other copy of
# com.timetracker.agent it finds (a dev build, a dragged copy in ~/Applications)
# and /Applications — the path the LaunchAgent runs — stays empty or stale.
pkgbuild --analyze --root "$ROOT_DIR" "$STAGING_PKG_DIR/component.plist" >/dev/null
plutil -replace 0.BundleIsRelocatable -bool NO "$STAGING_PKG_DIR/component.plist"

pkgbuild \
    --root "$ROOT_DIR" \
    --component-plist "$STAGING_PKG_DIR/component.plist" \
    --install-location /Applications \
    --scripts "$SCRIPTS_DIR" \
    --identifier "com.timetracker.agent" \
    --version "$VERSION" \
    --min-os-version "$MIN_MACOS" \
    "$COMPONENT_PKG"

# Product archive with an explicit distribution: hostArchitectures declares the
# package native on BOTH Apple Silicon and Intel (without it, Installer on an
# Apple Silicon Mac asks to install Rosetta first), and the OS-version check
# refuses cleanly on a Mac too old for the bundled Qt instead of installing an
# app that cannot launch.
cat > "$STAGING_PKG_DIR/distribution.xml" << DIST
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>TimeTracker ${VERSION}</title>
    <options customize="never" require-scripts="false" hostArchitectures="arm64,x86_64"/>
    <domains enable_localSystem="true"/>
    <allowed-os-versions>
        <os-version min="${MIN_MACOS}"/>
    </allowed-os-versions>
    <choices-outline>
        <line choice="default">
            <line choice="com.timetracker.agent"/>
        </line>
    </choices-outline>
    <choice id="default"/>
    <choice id="com.timetracker.agent" visible="false">
        <pkg-ref id="com.timetracker.agent"/>
    </choice>
    <pkg-ref id="com.timetracker.agent" version="${VERSION}" onConclusion="none">TimeTracker-component.pkg</pkg-ref>
</installer-gui-script>
DIST

productbuild \
    --distribution "$STAGING_PKG_DIR/distribution.xml" \
    --package-path "$STAGING_PKG_DIR" \
    "$PKG_PATH"

echo "Package built: $(basename "$PKG_PATH")"

# ── 5. Wrap the .pkg in a DMG ─────────────────────────────────────────────────
cp "$PKG_PATH" "$STAGING/"

DMG_PATH="$SCRIPT_DIR/TimeTracker-${VERSION}.dmg"
rm -f "$DMG_PATH"

# Explicit size: hdiutil's own -srcfolder estimate is sometimes too small and
# fails with "No space left on device" even with plenty of disk free.
DMG_SIZE_MB=$(( $(du -sm "$STAGING" | cut -f1) + 32 ))

hdiutil create \
    -volname "TimeTracker ${VERSION}" \
    -srcfolder "$STAGING" \
    -size "${DMG_SIZE_MB}m" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_PATH"

echo ""
echo "✓ Built: $DMG_PATH"
echo "  Size : $(du -sh "$DMG_PATH" | cut -f1)"
echo ""
echo "User flow: Open DMG → double-click the .pkg → click Install → app launches automatically."
