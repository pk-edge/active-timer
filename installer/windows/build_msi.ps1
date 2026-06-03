<#
.SYNOPSIS
    Builds a single self-contained TimeTracker MSI installer for Windows.

.DESCRIPTION
    Installs all prerequisites automatically, then produces:
        installer\windows\output\TimeTracker.msi

    Target machines need nothing pre-installed - the MSI bundles everything.
    The agent starts immediately after install and auto-starts on every login.

.PARAMETER CompanyToken
    (Required) Enrollment token from seedAdmin. Baked into the MSI.

.PARAMETER ApiBase
    Backend URL. Auto-detected from local network IP if not provided (port 8000).
    Override for production: -ApiBase "https://api.example.com"

.PARAMETER SkipPyInstaller
    Skip PyInstaller if agent\dist\timetracker\ already exists.

.EXAMPLE
    # Auto-detect API URL (dev/LAN — just run it):
    .\build_msi.ps1 -CompanyToken "abc123"

.EXAMPLE
    # Production with explicit URL:
    .\build_msi.ps1 -CompanyToken "abc123" -ApiBase "https://api.example.com"

.EXAMPLE
    # Silent deploy after build:
    msiexec /i output\TimeTracker.msi /quiet /qn /norestart
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true)]
    [string]$CompanyToken,

    [string]$ApiBase = "",

    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Resolve-Path "$ScriptDir\..\.."
$AgentDir   = Join-Path $RepoRoot "agent"
$DistDir    = Join-Path $AgentDir "dist\timetracker"
$OutputDir  = Join-Path $ScriptDir "output"
$TokenFile  = Join-Path $ScriptDir "enrollment_token.txt"
$WxsFile    = Join-Path $ScriptDir "TimeTracker.wxs"
$OutputMsi  = Join-Path $OutputDir "TimeTracker.msi"

function Write-Step([string]$msg) { Write-Host "`n  >> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "     OK  $msg" -ForegroundColor Green }
function Fail([string]$msg)       { Write-Host "`n  FAIL  $msg`n" -ForegroundColor Red; exit 1 }

# Auto-detect local IP if ApiBase not provided - useful for dev/LAN deployments
if (-not $ApiBase) {
    $localIp = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.PrefixOrigin -ne "WellKnown" -and
                       $_.IPAddress -notlike "169.254.*" } |
        Sort-Object -Property { $_.InterfaceAlias -like "Wi-Fi*" -or
                                 $_.InterfaceAlias -like "Ethernet*" } -Descending |
        Select-Object -First 1).IPAddress
    if ($localIp) {
        $ApiBase = "http://${localIp}:8000"
        Write-Host "  Auto-detected API base: $ApiBase" -ForegroundColor Yellow
    } else {
        $ApiBase = "http://localhost:8000"
        Write-Host "  WARNING: Could not detect local IP - using localhost:8000" -ForegroundColor Yellow
    }
}


# ── 1. Python ─────────────────────────────────────────────────────────────────

Write-Step "Checking Python"
try   { $v = python --version 2>&1; Write-Ok $v }
catch { Fail "Python 3.11+ not found. Install from https://python.org then re-run." }


# ── 2. Python dependencies (including pywin32 for window tracking) ────────────

Write-Step "Installing Python dependencies"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pywin32
python -m pip install --quiet -r "$AgentDir\requirements.txt"
Write-Ok "All dependencies installed"


# ── 3. WiX Toolset v4 ─────────────────────────────────────────────────────────

Write-Step "Checking WiX Toolset v4"
$wixOk = $false
try {
    $wixVer = & wix --version 2>&1
    if ($LASTEXITCODE -eq 0) { $wixOk = $true; Write-Ok "wix $wixVer" }
} catch {}

if (-not $wixOk) {
    Write-Host "     WiX not found - installing via dotnet tool..." -ForegroundColor Yellow

    try { dotnet --version | Out-Null }
    catch { Fail ".NET SDK not found. Install from https://dotnet.microsoft.com/download then re-run." }

    dotnet tool install --global wix
    if ($LASTEXITCODE -ne 0) { Fail "WiX installation failed. Try: dotnet tool install -g wix" }

    # Refresh PATH for current session
    $env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"

    try {
        $wixVer = & wix --version 2>&1
        Write-Ok "wix $wixVer installed"
    } catch {
        Fail "WiX installed but 'wix' command not found. Restart PowerShell and re-run."
    }
}


# ── 4. PyInstaller ────────────────────────────────────────────────────────────

if ($SkipPyInstaller -and (Test-Path $DistDir)) {
    Write-Step "Skipping PyInstaller (existing dist found)"
    Write-Ok $DistDir
} else {
    Write-Step "Building standalone bundle with PyInstaller (~2 min)"
    Push-Location $AgentDir
    try {
        python -m PyInstaller timetracker.spec --clean --noconfirm
        if ($LASTEXITCODE -ne 0) { Fail "PyInstaller exited with code $LASTEXITCODE" }
    } finally { Pop-Location }

    if (-not (Test-Path $DistDir)) { Fail "PyInstaller completed but dist\timetracker not found." }
    Write-Ok "Bundle ready"
}

# Bake API base into _internal/ so config.py finds it without relying on env var (matches macOS behaviour)
[System.IO.File]::WriteAllText(
    "$DistDir\_internal\api_base.txt",
    $ApiBase,
    (New-Object System.Text.UTF8Encoding $false)
)
Write-Ok "api_base.txt baked into _internal\"


# ── 5. Bake enrollment token ──────────────────────────────────────────────────

Write-Step "Embedding enrollment token"
[System.IO.File]::WriteAllText($TokenFile, $CompanyToken, (New-Object System.Text.UTF8Encoding $false))
Write-Ok "Token written (will be deleted after build)"


# ── 6. Build MSI ─────────────────────────────────────────────────────────────

Write-Step "Building MSI with WiX"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

try {
    & wix build $WxsFile `
        -acceptEula wix7 `
        -d "ApiBase=$ApiBase" `
        -o $OutputMsi

    if ($LASTEXITCODE -ne 0) { Fail "WiX build failed (exit code $LASTEXITCODE)" }
    if (-not (Test-Path $OutputMsi)) { Fail "WiX succeeded but MSI not found at $OutputMsi" }

    $sizeMb = [math]::Round((Get-Item $OutputMsi).Length / 1MB, 1)
} finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $TokenFile
}


# ── Done ──────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ================================================================" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host ""
Write-Host "    $OutputMsi  ($sizeMb MB)" -ForegroundColor White
    Write-Host "    Fully self-contained - nothing to install on target machine." -ForegroundColor White
Write-Host ""
Write-Host "  Employee install:" -ForegroundColor Green
    Write-Host "    Double-click TimeTracker.msi - no wizard, no prompts" -ForegroundColor White
Write-Host ""
Write-Host "  IT / MDM / SCCM silent deploy:" -ForegroundColor Green
Write-Host "    msiexec /i TimeTracker.msi /quiet /qn /norestart" -ForegroundColor White
Write-Host ""
Write-Host "  What happens after install:" -ForegroundColor Green
Write-Host "    - Agent starts immediately (floating timer top-right)" -ForegroundColor White
    Write-Host "    - Auto-starts on every Windows login - no manual action needed" -ForegroundColor White
Write-Host "  ================================================================" -ForegroundColor Green
Write-Host ""
