# TimeTracker Windows Build Script — v1.1.3 (WiX v7)
#
# Builds timetracker.exe once with PyInstaller, then packages TWO MSIs:
#   output\TimeTracker-staging.msi    → https://api.tracker.staging.onedge.co
#   output\TimeTracker-production.msi → https://api.tracker.onedge.co
#
# Usage:
#   cd active-time\installer\windows
#   .\build_windows.ps1
#   .\build_windows.ps1 -Only staging           # one environment only
#   .\build_windows.ps1 -Only production
#   .\build_windows.ps1 -SkipPyInstaller        # re-package existing dist\
#
# Prerequisites (install once):
#   1. Python 3.11 or 3.12 (python.org) — add to PATH
#   2. .NET SDK: https://dotnet.microsoft.com
#   3. WiX v7:
#        dotnet tool install --global wix
#        wix eula accept wix7
#        wix extension add WixToolset.UI.wixext/7.0.0 -g
#        wix extension add WixToolset.Util.wixext/7.0.0 -g

param(
    [string]$Only           = "",        # "staging" | "production" | "" (both)
    [string]$Version        = "1.1.3",
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentDir   = Join-Path $ScriptDir "..\..\agent"
$DistDir    = Join-Path $AgentDir  "dist\timetracker"
$OutputDir  = Join-Path $ScriptDir "output"
$WxsFile    = Join-Path $ScriptDir "TimeTracker.wxs"
$WixObjDir  = Join-Path $ScriptDir "wix_obj"
$FragFile   = Join-Path $WixObjDir "AgentFiles.wxs"

# ── Environment definitions ───────────────────────────────────────────────────
$Environments = [ordered]@{
    staging    = "https://api.tracker.staging.onedge.co"
    production = "https://api.tracker.onedge.co"
}

Write-Host "=== TimeTracker Windows Build v$Version ===" -ForegroundColor Cyan
Write-Host "Agent dir : $AgentDir"
Write-Host "Output    : $OutputDir"
if ($Only) { Write-Host "Only      : $Only" }

# ── 1. Virtual environment ────────────────────────────────────────────────────
if (-not $SkipPyInstaller) {
    Push-Location $AgentDir
    if (-not (Test-Path ".venv")) {
        Write-Host "`n[1/3] Creating virtual environment…" -ForegroundColor Yellow
        python -m venv .venv
    }
    Write-Host "[1/3] Installing requirements…" -ForegroundColor Yellow
    & .\.venv\Scripts\pip install --quiet --upgrade pip
    & .\.venv\Scripts\pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "pip install failed" }

    # ── 2. PyInstaller ────────────────────────────────────────────────────────
    Write-Host "`n[2/3] Running PyInstaller…" -ForegroundColor Yellow
    & .\.venv\Scripts\pyinstaller timetracker.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "PyInstaller failed" }
    Pop-Location
} else {
    Write-Host "`n[1-2/3] Skipping PyInstaller." -ForegroundColor DarkGray
}

if (-not (Test-Path $DistDir)) {
    throw "Dist directory not found at $DistDir — run without -SkipPyInstaller"
}
$DistDir = (Resolve-Path $DistDir).Path

# ── 3. Generate WiX fragment once (shared by both environments) ───────────────
Write-Host "`n[3/3] Packaging MSIs…" -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $WixObjDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir  | Out-Null

# Collect all files except timetracker.exe (handled by TimeTrackerExe component)
$allFiles = Get-ChildItem -Path $DistDir -Recurse -File |
    Where-Object { $_.Name -ne "timetracker.exe" -or $_.DirectoryName -ne $DistDir }

function New-DirNode($name, $relPath) {
    @{ name = $name; relPath = $relPath; children = [ordered]@{}; files = [System.Collections.ArrayList]::new() }
}
$root = New-DirNode '' ''

foreach ($f in $allFiles) {
    $rel   = $f.FullName.Substring($DistDir.Length).TrimStart('\')
    $parts = $rel -split '\\'
    $node  = $root
    for ($i = 0; $i -lt $parts.Count - 1; $i++) {
        $part = $parts[$i]
        if (-not $node.children.Contains($part)) {
            $parentRel = if ($node.relPath) { "$($node.relPath)\$part" } else { $part }
            $node.children[$part] = New-DirNode $part $parentRel
        }
        $node = $node.children[$part]
    }
    [void]$node.files.Add($rel)
}

function Get-DirId($relPath) {
    if (-not $relPath) { return 'INSTALLDIR' }
    "dir_" + ($relPath -replace '[\\\/\.\-\s\(\)\+\@\[\]\{\}]', '_' -replace '_+', '_').Trim('_')
}

$compLines = [System.Collections.ArrayList]::new()

function Write-DirTree($node, $sb, $baseIndent) {
    foreach ($child in $node.children.Values) {
        $dirId  = Get-DirId $child.relPath
        $indent = $baseIndent + '  '
        [void]$sb.AppendLine("$baseIndent<Directory Id=`"$dirId`" Name=`"$($child.name)`">")
        Write-DirTree $child $sb $indent
        [void]$sb.AppendLine("$baseIndent</Directory>")
    }
}

function Collect-Components($node) {
    $dirId = Get-DirId $node.relPath
    foreach ($rel in $node.files) {
        [void]$compLines.Add("      <Component Directory=`"$dirId`">")
        [void]$compLines.Add("        <File Source=`"`$(var.DistDir)\$rel`" />")
        [void]$compLines.Add("      </Component>")
    }
    foreach ($child in $node.children.Values) { Collect-Components $child }
}

$sb = [System.Text.StringBuilder]::new()
[void]$sb.AppendLine('<?xml version="1.0" encoding="UTF-8"?>')
[void]$sb.AppendLine('<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">')
[void]$sb.AppendLine('  <Fragment>')
[void]$sb.AppendLine()
[void]$sb.AppendLine('    <DirectoryRef Id="INSTALLDIR">')
Write-DirTree $root $sb '      '
[void]$sb.AppendLine('    </DirectoryRef>')
[void]$sb.AppendLine()
Collect-Components $root
[void]$sb.AppendLine('    <ComponentGroup Id="AgentFiles">')
foreach ($line in $compLines) { [void]$sb.AppendLine($line) }
[void]$sb.AppendLine('    </ComponentGroup>')
[void]$sb.AppendLine()
[void]$sb.AppendLine('  </Fragment>')
[void]$sb.AppendLine('</Wix>')
$sb.ToString() | Set-Content -Path $FragFile -Encoding UTF8

# ── Build one MSI per environment ─────────────────────────────────────────────
foreach ($env in $Environments.Keys) {
    if ($Only -and $Only -ne $env) { continue }

    $ApiBase = $Environments[$env]
    $MsiOut  = Join-Path $OutputDir "TimeTracker-${env}.msi"

    Write-Host ""
    Write-Host "  ── $env → $ApiBase" -ForegroundColor White

    # Write api_base.txt for this environment
    $apiFile = Join-Path $DistDir "_internal\api_base.txt"
    Set-Content -Path $apiFile -Value $ApiBase -Encoding UTF8
    Write-Host "    api_base.txt written"

    # Build MSI
    wix build `
        -d "Version=$Version" `
        -d "DistDir=$DistDir" `
        -ext WixToolset.UI.wixext `
        -ext WixToolset.Util.wixext `
        -out $MsiOut `
        $WxsFile $FragFile

    if ($LASTEXITCODE -ne 0) { throw "wix build failed for $env" }

    $size = [math]::Round((Get-Item $MsiOut).Length / 1MB, 1)
    Write-Host "    -> $MsiOut ($size MB)" -ForegroundColor Green
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
foreach ($env in $Environments.Keys) {
    if ($Only -and $Only -ne $env) { continue }
    $msi = Join-Path $OutputDir "TimeTracker-${env}.msi"
    if (Test-Path $msi) {
        Write-Host "  $env : $msi"
    }
}
Write-Host "Version : $Version"
