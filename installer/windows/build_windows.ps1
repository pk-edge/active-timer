# TimeTracker Windows Build Script — v1.0.2
# Run this on a Windows machine with Python 3.11+ installed.
# Output: installer\windows\output\TimeTracker.msi
#
# Prerequisites (install once):
#   1. Python 3.11 or 3.12 (python.org) — add to PATH
#   2. WiX Toolset v3.11  (wixtoolset.org/releases/) — adds candle.exe / light.exe to PATH
#   3. Visual C++ Redistributables (usually already present)
#
# Usage:
#   cd <repo>\active-time\installer\windows
#   .\build_windows.ps1
#   .\build_windows.ps1 -ApiBase "http://192.168.1.100:8000"

param(
    [string]$ApiBase = "http://10.10.20.77:8000",
    [string]$Version = "1.0.2"
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentDir   = Join-Path $ScriptDir "..\..\agent"
$DistDir    = Join-Path $AgentDir "dist\timetracker"
$OutputDir  = Join-Path $ScriptDir "output"
$WxsFile    = Join-Path $ScriptDir "TimeTracker.wxs"
$WixObjDir  = Join-Path $ScriptDir "wix_obj"

Write-Host "=== TimeTracker Windows Build v$Version ===" -ForegroundColor Cyan
Write-Host "API Base: $ApiBase"
Write-Host "Agent dir: $AgentDir"

# ── 1. Virtual environment ────────────────────────────────────────────────────
Push-Location $AgentDir
if (-not (Test-Path ".venv")) {
    Write-Host "`n[1/5] Creating virtual environment…" -ForegroundColor Yellow
    python -m venv .venv
}
& .\.venv\Scripts\pip install --quiet --upgrade pip
Write-Host "[1/5] Installing requirements…" -ForegroundColor Yellow
& .\.venv\Scripts\pip install --quiet -r requirements.txt

# ── 2. PyInstaller ────────────────────────────────────────────────────────────
Write-Host "`n[2/5] Running PyInstaller…" -ForegroundColor Yellow
& .\.venv\Scripts\pyinstaller timetracker.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# ── 3. Write api_base.txt ─────────────────────────────────────────────────────
Write-Host "`n[3/5] Writing api_base.txt ($ApiBase)…" -ForegroundColor Yellow
$apiFile = Join-Path $DistDir "_internal\api_base.txt"
Set-Content -Path $apiFile -Value $ApiBase -Encoding UTF8
Write-Host "    → $apiFile"

Pop-Location

# ── 4. WiX — compile .wxs → .wixobj ──────────────────────────────────────────
Write-Host "`n[4/5] Compiling WiX installer…" -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $OutputDir  | Out-Null
New-Item -ItemType Directory -Force -Path $WixObjDir  | Out-Null

# Generate file list from dist directory
$HeatOut = Join-Path $WixObjDir "AgentFiles.wxs"
heat dir $DistDir `
    -nologo -gg -g1 -srd -sreg -ke -dr INSTALLDIR `
    -cg AgentFiles `
    -var var.DistDir `
    -out $HeatOut
if ($LASTEXITCODE -ne 0) { throw "heat failed" }

candle -nologo `
    -dVersion="$Version" `
    -dDistDir="$DistDir" `
    -out "$WixObjDir\" `
    $WxsFile $HeatOut
if ($LASTEXITCODE -ne 0) { throw "candle failed" }

$WixObjs = (Get-ChildItem "$WixObjDir\*.wixobj").FullName -join " "
light -nologo `
    -ext WixUIExtension `
    -out (Join-Path $OutputDir "TimeTracker.msi") `
    -b $DistDir `
    $WixObjs
if ($LASTEXITCODE -ne 0) { throw "light failed" }

# ── 5. Done ───────────────────────────────────────────────────────────────────
$msi = Join-Path $OutputDir "TimeTracker.msi"
$size = [math]::Round((Get-Item $msi).Length / 1MB, 1)
Write-Host "`n=== Build complete ===" -ForegroundColor Green
Write-Host "MSI : $msi ($size MB)"
Write-Host "Version : $Version"
Write-Host "API Base: $ApiBase"
