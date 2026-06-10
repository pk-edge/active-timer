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
#   ./build_dmg.sh                       — uses enrollment_token.txt + backends.json if present
#   ./build_dmg.sh <token> [<api_url>]   — embeds the given token and optional API URL
#
# Prerequisites: Python venv with pyinstaller, pkgbuild + hdiutil (built-in macOS)
#
# Output: dist/TimeTracker-<version>.dmg  (contains a .pkg installer inside)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"

if [ ! -x "$VENV/bin/pyinstaller" ]; then
    echo "ERROR: PyInstaller not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
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
trap 'rm -rf "$SCRIPTS_DIR" "$ROOT_DIR" "$STAGING"' EXIT

# ── 1. PyInstaller ────────────────────────────────────────────────────────────
"$VENV/bin/pyinstaller" timetracker.spec --noconfirm
echo "PyInstaller complete."

# ── 2. Embed config files into the app bundle ─────────────────────────────────
RESOURCES="$SCRIPT_DIR/dist/TimeTracker.app/Contents/Resources"

if [ -n "$ARG_TOKEN" ]; then
    printf '%s' "$ARG_TOKEN" > "$RESOURCES/enrollment_token.txt"
    echo "Embedded enrollment token from argument."
elif [ -f "$SCRIPT_DIR/enrollment_token.txt" ]; then
    cp "$SCRIPT_DIR/enrollment_token.txt" "$RESOURCES/"
    echo "Embedded enrollment_token.txt."
fi

if [ -n "$ARG_URL" ]; then
    printf '%s' "$ARG_URL" > "$RESOURCES/api_base.txt"
    echo "Embedded api_base.txt."
fi

if [ -f "$SCRIPT_DIR/backends.json" ]; then
    cp "$SCRIPT_DIR/backends.json" "$RESOURCES/"
    echo "Embedded backends.json."
fi

# ── 3. Installer scripts ──────────────────────────────────────────────────────

# preinstall: quit any running instance + clear stale localhost identity
cat > "$SCRIPTS_DIR/preinstall" << 'EOF'
#!/bin/bash
# Quit any running instance gracefully before upgrade
pkill -x "timetracker" 2>/dev/null || true
sleep 1

# If the stored backend is localhost (dev/misconfigured install), wipe the
# identity so the agent re-enrolls against the correct production backend.
# We check every real user's home dir because the installer runs as root.
for HOME_DIR in /Users/*/; do
    IDENTITY="$HOME_DIR/Library/Application Support/TimeTracker/identity.json"
    if [ -f "$IDENTITY" ] && grep -q '"api_base".*localhost' "$IDENTITY" 2>/dev/null; then
        rm -f "$IDENTITY"
        echo "Cleared stale localhost identity at $IDENTITY"
    fi
done

exit 0
EOF

# postinstall: launch the app for the current GUI user right after install
cat > "$SCRIPTS_DIR/postinstall" << 'EOF'
#!/bin/bash
# Find the logged-in GUI user (installer runs as root).
LOGGED_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")

if [ -z "$LOGGED_USER" ] || [ "$LOGGED_USER" = "root" ] || [ "$LOGGED_USER" = "loginwindow" ]; then
    exit 0
fi

USER_UID=$(id -u "$LOGGED_USER" 2>/dev/null || echo "")
[ -z "$USER_UID" ] && exit 0

# Set up LaunchAgent for auto-start on login (idempotent).
PLIST_DIR="/Users/$LOGGED_USER/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.timetracker.agent.plist"
mkdir -p "$PLIST_DIR"

cat > "$PLIST" << PLIST
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
    <key>ProcessType</key>
    <string>Interactive</string>
  </dict>
</plist>
PLIST

chown "$LOGGED_USER" "$PLIST"

# Launch immediately in the user's GUI session.
launchctl asuser "$USER_UID" open -a /Applications/TimeTracker.app

exit 0
EOF

chmod +x "$SCRIPTS_DIR/preinstall" "$SCRIPTS_DIR/postinstall"

# ── 4. Build the .pkg installer ───────────────────────────────────────────────
# pkgbuild --root copies everything from ROOT_DIR into --install-location.
# So we place TimeTracker.app inside ROOT_DIR → it lands at /Applications/TimeTracker.app.
cp -R "$SCRIPT_DIR/dist/TimeTracker.app" "$ROOT_DIR/"

PKG_PATH="$SCRIPT_DIR/dist/TimeTracker-${VERSION}.pkg"

pkgbuild \
    --root "$ROOT_DIR" \
    --install-location /Applications \
    --scripts "$SCRIPTS_DIR" \
    --identifier "com.timetracker.agent" \
    --version "$VERSION" \
    "$PKG_PATH"

echo "Package built: $(basename "$PKG_PATH")"

# ── 5. Wrap the .pkg in a DMG ─────────────────────────────────────────────────
cp "$PKG_PATH" "$STAGING/"

DMG_PATH="$SCRIPT_DIR/dist/TimeTracker-${VERSION}.dmg"
rm -f "$DMG_PATH"

hdiutil create \
    -volname "TimeTracker ${VERSION}" \
    -srcfolder "$STAGING" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_PATH"

echo ""
echo "✓ Built: $DMG_PATH"
echo "  Size : $(du -sh "$DMG_PATH" | cut -f1)"
echo ""
echo "User flow: Open DMG → double-click the .pkg → click Install → app launches automatically."
