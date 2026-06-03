#!/bin/bash
# TimeTracker macOS DMG builder — one command, fully self-contained output
#
# Usage:
#   cd installer/macos
#   COMPANY_TOKEN="your-token" ./build_dmg.sh              # auto-detects local IP
#   COMPANY_TOKEN="your-token" API_BASE="https://api.example.com" ./build_dmg.sh  # production
#
# Output: installer/macos/TimeTracker.dmg
#
# Install experience (employee):
#   1. Open TimeTracker.dmg
#   2. Drag TimeTracker → Applications
#   3. Launch TimeTracker once from Applications
#      → agent starts, floating timer appears, auto-start registered
#   4. From every login onwards: agent starts automatically
#
# The DMG is fully self-contained — no Python, no dependencies needed on the target Mac.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"
AGENT_DIST="$AGENT_DIR/dist/TimeTracker.app"
APP_NAME="TimeTracker"
DMG_NAME="TimeTracker"
VOLUME_NAME="TimeTracker"
VERSION="1.0.0"
OUTPUT_DMG="$(cd "$(dirname "$0")" && pwd)/${DMG_NAME}.dmg"

COMPANY_TOKEN="${COMPANY_TOKEN:-}"

# Auto-detect local IP if API_BASE not set — works for dev deployments where
# the backend is running on the same machine that built the DMG.
if [ -z "${API_BASE:-}" ]; then
  _IP=$(ipconfig getifaddr en0 2>/dev/null \
     || ipconfig getifaddr en1 2>/dev/null \
     || ipconfig getifaddr en2 2>/dev/null \
     || echo "")
  if [ -n "$_IP" ]; then
    API_BASE="http://$_IP:8000"
  else
    API_BASE="http://localhost:8000"
    echo "  WARNING: Could not detect local IP — falling back to localhost:8000"
  fi
fi

# ── Validate ──────────────────────────────────────────────────────────────────
if [ -z "$COMPANY_TOKEN" ]; then
  echo ""
  echo "  ERROR: COMPANY_TOKEN is required."
  echo ""
  echo "  Get it by running seedAdmin on your backend, then:"
  echo "    COMPANY_TOKEN=<token> API_BASE=https://api.example.com ./build_dmg.sh"
  echo ""
  exit 1
fi

echo ""
echo "  Building TimeTracker.dmg"
echo "    Version  : $VERSION"
echo "    API base : $API_BASE"
echo "    Token    : ${COMPANY_TOKEN:0:8}..."
echo ""

# ── Step 1: Build .app with PyInstaller (if not already built) ────────────────
if [ ! -d "$AGENT_DIST" ]; then
  echo "  Step 1/3 — Building standalone .app bundle (first run, ~2 min)..."
  echo ""

  cd "$AGENT_DIR"

  # Use the project venv if present, otherwise fall back to system Python
  if [ -f ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
    PIP=".venv/bin/pip"
  else
    PYTHON="python3"
    PIP="pip3"
    echo "  Installing Python dependencies..."
    $PIP install --quiet -r requirements.txt
  fi

  $PYTHON -m PyInstaller timetracker.spec --clean --noconfirm

  if [ ! -d "$AGENT_DIST" ]; then
    echo ""
    echo "  ERROR: PyInstaller finished but dist/TimeTracker.app was not created."
    exit 1
  fi

  cd - > /dev/null
  echo "  .app bundle ready."
  echo ""
else
  echo "  Step 1/3 — Using existing .app at $AGENT_DIST"
fi

# ── Step 2: Stage DMG contents ────────────────────────────────────────────────
echo "  Step 2/3 — Staging DMG contents..."

WORK_DIR="$(mktemp -d)/dmg_staging"
mkdir -p "$WORK_DIR"
trap 'rm -rf "$(dirname "$WORK_DIR")"' EXIT

# Copy .app
cp -r "$AGENT_DIST" "$WORK_DIR/$APP_NAME.app"

# Embed enrollment token and API base as plain text files in Resources.
# Writing plain files avoids modifying Info.plist after PyInstaller has signed
# the bundle, which would break the code signature and prevent macOS from opening the app.
RESOURCES="$WORK_DIR/$APP_NAME.app/Contents/Resources"
mkdir -p "$RESOURCES"
printf '%s' "$COMPANY_TOKEN" > "$RESOURCES/enrollment_token.txt"
printf '%s' "$API_BASE"      > "$RESOURCES/api_base.txt"

# Applications symlink for drag-to-install UX
ln -s /Applications "$WORK_DIR/Applications"

# ── Step 3: Create DMG ────────────────────────────────────────────────────────
echo "  Step 3/3 — Creating DMG..."

RW_DMG="/tmp/${DMG_NAME}_rw.dmg"

APP_SIZE_KB=$(du -sk "$WORK_DIR/$APP_NAME.app" | cut -f1)
DMG_SIZE_MB=$(( (APP_SIZE_KB / 1024) + 20 ))

hdiutil create \
  -volname "$VOLUME_NAME" \
  -srcfolder "$WORK_DIR" \
  -ov \
  -format UDRW \
  -size "${DMG_SIZE_MB}m" \
  "$RW_DMG" \
  > /dev/null

# Mount and set Finder window layout
MOUNT_DIR="/Volumes/$VOLUME_NAME"
[ -d "$MOUNT_DIR" ] && hdiutil detach "$MOUNT_DIR" -force > /dev/null 2>&1 || true
hdiutil attach "$RW_DMG" -mountpoint "$MOUNT_DIR" -nobrowse -quiet

osascript << APPLESCRIPT 2>/dev/null || true
tell application "Finder"
  tell disk "$VOLUME_NAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 100, 700, 400}
    set viewOptions to the icon view options of container window
    set arrangement of viewOptions to not arranged
    set icon size of viewOptions to 128
    set position of item "$APP_NAME.app"  of container window to {120, 140}
    set position of item "Applications"   of container window to {380, 140}
    close
    open
    update without registering applications
    delay 2
    close
  end tell
end tell
APPLESCRIPT

sync
sleep 2
hdiutil detach "$MOUNT_DIR" -quiet 2>/dev/null || hdiutil detach "$MOUNT_DIR" -force 2>/dev/null || true

# Convert to compressed read-only DMG
rm -f "$OUTPUT_DMG"
hdiutil convert "$RW_DMG" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -o "$OUTPUT_DMG" \
  > /dev/null
rm -f "$RW_DMG"

DMG_SIZE=$(du -sh "$OUTPUT_DMG" | cut -f1)

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ================================================================"
echo "  Build complete!"
echo ""
echo "    $OUTPUT_DMG  ($DMG_SIZE)"
echo "    Fully self-contained — nothing to install on target Mac."
echo ""
echo "  How to distribute:"
echo "    Send TimeTracker.dmg to employees via email / Slack / shared drive."
echo ""
echo "  Employee install:"
echo "    1. Open TimeTracker.dmg"
echo "    2. Drag TimeTracker → Applications"
echo "    3. Launch TimeTracker once — agent starts, timer appears"
echo "    4. Auto-starts on every login from here on"
echo "  ================================================================"
echo ""
