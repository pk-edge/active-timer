#!/usr/bin/env bash
# bash 4+ required for associative arrays (Homebrew: brew install bash)
# TimeTracker macOS Build Script
#
# Builds TimeTracker.app once with PyInstaller, then packages TWO DMGs:
#   TimeTracker-staging.dmg    → https://api.tracker.staging.onedge.co
#   TimeTracker-production.dmg → https://api.tracker.onedge.co
#
# Usage:
#   cd active-time/installer/macos
#   ./build_macos.sh
#   ./build_macos.sh --skip-pyinstaller        # re-package existing dist/
#   ./build_macos.sh --only staging            # one environment only
#   ./build_macos.sh --only production
#
# Requirements:
#   - Python 3.11+ (python3 on PATH)
#   - macOS 12+, Xcode CLT: xcode-select --install
#   - hdiutil (built-in on macOS)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../../agent" && pwd)"
DIST_APP="$AGENT_DIR/dist/TimeTracker.app"
VERSION="${VERSION:-1.1.3}"
SKIP_PYINSTALLER=false
ONLY=""

# ── Environments: parallel arrays (bash 3.2 compatible) ──────────────────────
ENVS=(staging production)
URL_staging="https://api.tracker.staging.onedge.co"
URL_production="https://api.tracker.onedge.co"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-pyinstaller) SKIP_PYINSTALLER=true ;;
    --only)             shift; ONLY="${1:-}" ;;
    --version=*)        VERSION="${1#*=}" ;;
    --version)          shift; VERSION="${1:-}" ;;
  esac
  shift
done

echo "=== TimeTracker macOS Build v$VERSION ==="

# ── 1. Virtual environment + PyInstaller ──────────────────────────────────────
if [ "$SKIP_PYINSTALLER" = false ]; then
  cd "$AGENT_DIR"

  if [ ! -d ".venv" ]; then
    echo -e "\n[1/3] Creating virtual environment…"
    python3 -m venv .venv
  fi

  echo "[1/3] Installing requirements…"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt

  echo -e "\n[2/3] Running PyInstaller…"
  .venv/bin/pyinstaller timetracker.spec --clean --noconfirm

  if [ ! -d "$DIST_APP" ]; then
    echo "ERROR: PyInstaller did not produce $DIST_APP" >&2
    exit 1
  fi
else
  echo -e "\n[1-2/3] Skipping PyInstaller."
  if [ ! -d "$DIST_APP" ]; then
    echo "ERROR: $DIST_APP not found — run without --skip-pyinstaller first." >&2
    exit 1
  fi
fi

# Resource dir inside the frozen macOS bundle (matches config.py logic)
RESOURCES="$DIST_APP/Contents/Resources"

# ── 2. Package a DMG for each environment ────────────────────────────────────
echo -e "\n[3/3] Packaging DMGs…"

_build_dmg() {
  local env="$1"          # staging | production
  local url="$2"          # API base URL
  local dmg_out="$SCRIPT_DIR/TimeTracker-${env}.dmg"

  echo ""
  echo "  ── $env → $url"

  # Write api_base.txt into a temporary copy of the app
  local stage
  stage="$(mktemp -d)"
  trap "rm -rf '$stage'" RETURN

  cp -R "$DIST_APP" "$stage/TimeTracker.app"
  echo -n "$url" > "$stage/TimeTracker.app/Contents/Resources/api_base.txt"

  # Re-sign with ad-hoc identity so macOS Gatekeeper is satisfied locally
  codesign --force --deep --sign - "$stage/TimeTracker.app" 2>/dev/null \
    && echo "    signed (ad-hoc)" \
    || echo "    WARNING: codesign failed — app may show Gatekeeper warning"

  # Add /Applications symlink for drag-install UX
  ln -s /Applications "$stage/Applications"

  rm -f "$dmg_out"
  hdiutil create \
    -volname "TimeTracker" \
    -srcfolder "$stage" \
    -ov \
    -format UDZO \
    -fs HFS+ \
    "$dmg_out" 2>&1 | grep -v "^hdiutil:"

  local size
  size=$(du -sh "$dmg_out" | cut -f1)
  echo "    -> $dmg_out ($size)"
}

for env in "${ENVS[@]}"; do
  [ -n "$ONLY" ] && [ "$ONLY" != "$env" ] && continue
  url_var="URL_${env}"
  _build_dmg "$env" "${!url_var}"
done

echo -e "\n=== Build complete ==="
for env in "${ENVS[@]}"; do
  [ -n "$ONLY" ] && [ "$ONLY" != "$env" ] && continue
  dmg="$SCRIPT_DIR/TimeTracker-${env}.dmg"
  [ -f "$dmg" ] && echo "  $env : $dmg"
done
echo "Version : $VERSION"
