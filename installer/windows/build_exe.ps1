<#
.SYNOPSIS
    Builds a self-contained TimeTracker installer (.exe) using Inno Setup.

.DESCRIPTION
    - Builds the Python agent with PyInstaller
    - Injects enrollment token + API base
    - Creates a silent installer using Inno Setup

.PARAMETER CompanyToken
    Enrollment token from seedAdmin.

.PARAMETER ApiBase
    Backend API URL.

.PARAMETER SkipPyInstaller
    Skip PyInstaller build if dist already exists.

.EXAMPLE
    .\build_exe.ps1 -CompanyToken "abc123"

.EXAMPLE
    .\build_exe.ps1 `
        -CompanyToken "abc123" `
        -ApiBase "http://10.10.21.230:8000"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CompanyToken,

    [string]$ApiBase = "",

    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Resolve-Path "$ScriptDir\..\.."

$AgentDir  = Join-Path $RepoRoot "agent"
$DistDir   = Join-Path $AgentDir "dist\timetracker"

$OutputDir = Join-Path $ScriptDir "output"
$IssFile   = Join-Path $ScriptDir "TimeTracker.iss"

# ──────────────────────────────────────────────────────────────────────────────
# Detect Inno Setup Automatically
# ──────────────────────────────────────────────────────────────────────────────

$PossibleIsccPaths = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 5\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 5\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 5\ISCC.exe"
)

$IsccExe = $null

foreach ($path in $PossibleIsccPaths) {
    if (Test-Path $path) {
        $IsccExe = $path
        break
    }
}

if (-not $IsccExe) {
    $IsccExe = "C:\Program Files\Inno Setup 6\ISCC.exe"
}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

function Step($msg) {
    Write-Host ""
    Write-Host ">> $msg" -ForegroundColor Cyan
}

function Ok($msg) {
    Write-Host "   OK  $msg" -ForegroundColor Green
}

function Fail($msg) {
    Write-Host ""
    Write-Host "FAIL  $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ──────────────────────────────────────────────────────────────────────────────
# Auto-detect API Base
# ──────────────────────────────────────────────────────────────────────────────

if (-not $ApiBase) {

    $ip = (
        Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.PrefixOrigin -ne "WellKnown" -and
            $_.IPAddress -notlike "169.254.*"
        } |
        Sort-Object {
            $_.InterfaceAlias -like "Wi-Fi*" -or
            $_.InterfaceAlias -like "Ethernet*"
        } -Descending |
        Select-Object -First 1
    ).IPAddress

    if ($ip) {
        $ApiBase = "http://${ip}:8000"
    }
    else {
        $ApiBase = "http://localhost:8000"
    }

    Write-Host "Auto-detected API base: $ApiBase" -ForegroundColor Yellow
}

# ──────────────────────────────────────────────────────────────────────────────
# 1. Check Python
# ──────────────────────────────────────────────────────────────────────────────

Step "Checking Python"

try {
    $v = python --version 2>&1
    Ok $v
}
catch {
    Fail "Python 3.11+ not found. Install from https://python.org"
}

# ──────────────────────────────────────────────────────────────────────────────
# 2. Install Python Dependencies
# ──────────────────────────────────────────────────────────────────────────────

Step "Installing Python dependencies"

python -m pip install --quiet --upgrade pip

if ($LASTEXITCODE -ne 0) {
    Fail "Failed to upgrade pip"
}

python -m pip install --quiet pywin32

if ($LASTEXITCODE -ne 0) {
    Fail "Failed to install pywin32"
}

python -m pip install --quiet -r "$AgentDir\requirements.txt"

if ($LASTEXITCODE -ne 0) {
    Fail "Failed to install requirements.txt"
}

Ok "Dependencies ready"

# ──────────────────────────────────────────────────────────────────────────────
# 3. Check Inno Setup
# ──────────────────────────────────────────────────────────────────────────────

Step "Checking Inno Setup"

if (-not (Test-Path $IsccExe)) {

    Write-Host "   Inno Setup not found - installing via winget..." -ForegroundColor Yellow

    try {

        winget install --id JRSoftware.InnoSetup --silent `
            --accept-package-agreements `
            --accept-source-agreements

        # Re-check after install
        foreach ($path in $PossibleIsccPaths) {
            if (Test-Path $path) {
                $IsccExe = $path
                break
            }
        }

        if (-not (Test-Path $IsccExe)) {
            Fail "Inno Setup installed but ISCC.exe still not found. Try opening a new PowerShell window."
        }
    }
    catch {
        Fail "winget failed. Install manually from https://jrsoftware.org/isinfo.php"
    }
}

Ok "ISCC.exe found at: $IsccExe"

# ──────────────────────────────────────────────────────────────────────────────
# 4. Build PyInstaller Bundle
# ──────────────────────────────────────────────────────────────────────────────

if ($SkipPyInstaller -and (Test-Path $DistDir)) {

    Step "Skipping PyInstaller build"
    Ok $DistDir
}
else {

    Step "Building standalone bundle with PyInstaller"

    Push-Location $AgentDir

    try {

        python -m PyInstaller timetracker.spec --clean --noconfirm

        if ($LASTEXITCODE -ne 0) {
            Fail "PyInstaller failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    if (-not (Test-Path $DistDir)) {
        Fail "PyInstaller completed but dist\timetracker not found."
    }

    Ok "Bundle ready"
}

# ──────────────────────────────────────────────────────────────────────────────
# 5. Bake Enrollment Token + API Base
# ──────────────────────────────────────────────────────────────────────────────

Step "Baking enrollment token and API base"

$InternalDir = Join-Path $DistDir "_internal"

if (-not (Test-Path $InternalDir)) {
    New-Item -ItemType Directory -Force -Path $InternalDir | Out-Null
}

$TokenFile = Join-Path $InternalDir "enrollment_token.txt"
$ApiFile   = Join-Path $InternalDir "api_base.txt"

[System.IO.File]::WriteAllText(
    $TokenFile,
    $CompanyToken,
    [System.Text.Encoding]::ASCII
)

[System.IO.File]::WriteAllText(
    $ApiFile,
    $ApiBase,
    [System.Text.Encoding]::ASCII
)

Ok "Configuration files written"

# ──────────────────────────────────────────────────────────────────────────────
# 6. Verify Inno Script Exists
# ──────────────────────────────────────────────────────────────────────────────

Step "Checking Inno Setup script"

if (-not (Test-Path $IssFile)) {
    Fail "Missing TimeTracker.iss at: $IssFile"
}

Ok "Found TimeTracker.iss"

# ──────────────────────────────────────────────────────────────────────────────
# 7. Build Installer
# ──────────────────────────────────────────────────────────────────────────────

Step "Building installer with Inno Setup"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

& $IsccExe $IssFile

if ($LASTEXITCODE -ne 0) {
    Fail "Inno Setup build failed with exit code $LASTEXITCODE"
}

# ──────────────────────────────────────────────────────────────────────────────
# 8. Verify Output
# ──────────────────────────────────────────────────────────────────────────────

$OutputExe = Join-Path $OutputDir "TimeTracker-Setup.exe"

if (-not (Test-Path $OutputExe)) {
    Fail "Installer EXE not found after build."
}

$sizeMb = [math]::Round((Get-Item $OutputExe).Length / 1MB, 1)

# ──────────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Green
Write-Host "Build complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Installer:" -ForegroundColor Green
Write-Host "  $OutputExe"
Write-Host "  Size: $sizeMb MB"
Write-Host ""
Write-Host "Employee install:" -ForegroundColor Green
Write-Host "  Double-click TimeTracker-Setup.exe"
Write-Host ""
Write-Host "Silent deployment:" -ForegroundColor Green
Write-Host "  TimeTracker-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"
Write-Host ""
Write-Host "===============================================================" -ForegroundColor Green
Write-Host ""