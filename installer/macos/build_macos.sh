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

# ── 3. Installer scripts ──────────────────────────────────────────────────────

# preinstall: quit any running instance + clear stale localhost identity
cat > "$SCRIPTS_DIR/preinstall" << 'EOF'
#!/bin/bash
# Quit any running instance gracefully before upgrade
pkill -x "timetracker" 2>/dev/null || true
sleep 1

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
cp -R "$AGENT_DIR/dist/TimeTracker.app" "$ROOT_DIR/"

PKG_PATH="$AGENT_DIR/dist/TimeTracker-${VERSION}.pkg"

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

DMG_PATH="$SCRIPT_DIR/TimeTracker-${VERSION}.dmg"
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
