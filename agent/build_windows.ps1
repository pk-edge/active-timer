# Build TimeTracker Windows MSI installer
#
# Usage:
#   .\build_windows.ps1                           # reads enrollment_token.txt / backends.json from cwd
#   .\build_windows.ps1 -Token <tok> -ApiUrl <url>  # embeds token + URL directly
#
# Prerequisites (run once):
#   pip install -r requirements.txt
#   dotnet tool install --global wix
#   wix extension add WixToolset.UI.wixext
#
# Output: dist\TimeTracker-<version>.msi

param(
    [string]$Token  = "",
    [string]$ApiUrl = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ── Version ───────────────────────────────────────────────────────────────────
$Version = python -c "from version import __version__; print(__version__)"
Write-Host "Building TimeTracker v$Version for Windows ..."

# ── 1. PyInstaller ────────────────────────────────────────────────────────────
python -m PyInstaller timetracker.spec --noconfirm
Write-Host "PyInstaller complete."

# ── 2. Embed config files into _internal ─────────────────────────────────────
# PyInstaller 6.x puts bundled resources under dist\timetracker\_internal\
$Internal = "dist\timetracker\_internal"
if (-not (Test-Path $Internal)) {
    # Older PyInstaller puts everything beside the exe
    $Internal = "dist\timetracker"
}

if ($Token) {
    $Token | Set-Content "$Internal\enrollment_token.txt" -Encoding UTF8
    Write-Host "Embedded enrollment token from argument."
} elseif (Test-Path "enrollment_token.txt") {
    Copy-Item "enrollment_token.txt" "$Internal\" -Force
    Write-Host "Embedded enrollment_token.txt."
}

if ($ApiUrl) {
    $ApiUrl | Set-Content "$Internal\api_base.txt" -Encoding UTF8
    Write-Host "Embedded api_base.txt from argument."
}

if (Test-Path "backends.json") {
    Copy-Item "backends.json" "$Internal\" -Force
    Write-Host "Embedded backends.json."
}

# ── 3. Harvest files and build MSI with WiX 4 ────────────────────────────────
$WixAvailable = $null -ne (Get-Command wix -ErrorAction SilentlyContinue)
if (-not $WixAvailable) {
    Write-Warning "WiX v4 not found. Install with: dotnet tool install --global wix"
    Write-Warning "Then run: wix extension add WixToolset.UI.wixext"
    Write-Host "PyInstaller output is ready at dist\timetracker\ — package manually."
    exit 1
}

# Harvest the dist folder into a generated WXS (auto-creates component entries)
Write-Host "Harvesting dist\timetracker\ ..."
& wix harvest dir "dist\timetracker" `
    --component-group-name AppFiles `
    --directory-ref INSTALLFOLDER `
    --suppress-unique-ids `
    -out "installer\AppFiles.wxs"
if ($LASTEXITCODE -ne 0) { throw "wix harvest failed" }

# Build the MSI
$MsiPath = "dist\TimeTracker-$Version.msi"
Write-Host "Building MSI ..."
& wix build `
    "installer\TimeTracker.wxs" `
    "installer\AppFiles.wxs" `
    -arch x64 `
    -ext WixToolset.UI.wixext `
    -define "Version=$Version" `
    -out $MsiPath
if ($LASTEXITCODE -ne 0) { throw "wix build failed" }

Write-Host ""
Write-Host "✓ Built: $MsiPath"
Write-Host "  Size : $([math]::Round((Get-Item $MsiPath).Length / 1MB, 1)) MB"
