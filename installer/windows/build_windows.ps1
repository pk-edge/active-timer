# TimeTracker Windows Build Script — v1.0.3 (WiX v4/v7)
# Run this on a Windows machine with Python 3.11+ installed.
# Output: installer\windows\output\TimeTracker.msi
#
# Prerequisites (install once):
#   1. Python 3.11 or 3.12 (python.org) — add to PATH
#   2. .NET SDK (for dotnet tool wix) — https://dotnet.microsoft.com
#   3. WiX Toolset v7: dotnet tool install --global wix
#      Then accept EULA: wix eula accept wix7
#      Then add extensions:
#        wix extension add WixToolset.UI.wixext/7.0.0 -g
#        wix extension add WixToolset.Util.wixext/7.0.0 -g
#
# Usage:
#   cd <repo>\active-time\installer\windows
#   .\build_windows.ps1
#   .\build_windows.ps1 -ApiBase "http://192.168.1.100:8000"

param(
    [string]$ApiBase  = "http://10.10.20.77:8000",
    [string]$Version  = "1.0.3",
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentDir   = Join-Path $ScriptDir "..\..\agent"
$DistDir    = Join-Path $AgentDir "dist\timetracker"
$OutputDir  = Join-Path $ScriptDir "output"
$WxsFile    = Join-Path $ScriptDir "TimeTracker.wxs"
$WixObjDir  = Join-Path $ScriptDir "wix_obj"
$FragFile   = Join-Path $WixObjDir "AgentFiles.wxs"

Write-Host "=== TimeTracker Windows Build v$Version ===" -ForegroundColor Cyan
Write-Host "API Base : $ApiBase"
Write-Host "Agent dir: $AgentDir"

# ── 1. Virtual environment ────────────────────────────────────────────────────
if (-not $SkipPyInstaller) {
    Push-Location $AgentDir
    if (-not (Test-Path ".venv")) {
        Write-Host "`n[1/5] Creating virtual environment…" -ForegroundColor Yellow
        python -m venv .venv
    }
    & .\.venv\Scripts\pip install --quiet --upgrade pip
    Write-Host "[1/5] Installing requirements…" -ForegroundColor Yellow
    & .\.venv\Scripts\pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "pip install failed" }

    # ── 2. PyInstaller ────────────────────────────────────────────────────────
    Write-Host "`n[2/5] Running PyInstaller…" -ForegroundColor Yellow
    & .\.venv\Scripts\pyinstaller timetracker.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "PyInstaller failed" }
    Pop-Location
} else {
    Write-Host "`n[1-2/5] Skipping PyInstaller (SkipPyInstaller flag set)" -ForegroundColor DarkGray
}

if (-not (Test-Path $DistDir)) {
    throw "Dist directory not found at $DistDir - run without -SkipPyInstaller"
}
# Resolve to absolute path so substring calculations match $f.FullName
$DistDir = (Resolve-Path $DistDir).Path

# ── 3. Write api_base.txt ─────────────────────────────────────────────────────
Write-Host "`n[3/5] Writing api_base.txt ($ApiBase)…" -ForegroundColor Yellow
$apiFile = Join-Path $DistDir "_internal\api_base.txt"
Set-Content -Path $apiFile -Value $ApiBase -Encoding UTF8
Write-Host "    -> $apiFile"

# ── 4. Generate WiX fragment (replaces `heat dir`) ────────────────────────────
Write-Host "`n[4/5] Generating WiX file fragment…" -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $WixObjDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir  | Out-Null

# Collect all files, excluding timetracker.exe (already in TimeTrackerExe component)
$allFiles = Get-ChildItem -Path $DistDir -Recurse -File |
    Where-Object { $_.Name -ne "timetracker.exe" -or $_.DirectoryName -ne $DistDir }

# Build directory tree as nested hashtable: { name, relPath, children: {}, files: [] }
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

# Generate a safe WiX Id from a relative directory path
function Get-DirId($relPath) {
    if (-not $relPath) { return 'INSTALLDIR' }
    "dir_" + ($relPath -replace '[\\\/\.\-\s\(\)\+\@\[\]\{\}]', '_' -replace '_+', '_').Trim('_')
}

# Recursively emit <Directory> XML (properly nested) and collect component lines
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
    foreach ($child in $node.children.Values) {
        Collect-Components $child
    }
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
$fileCount = $allFiles.Count
Write-Host "    -> $FragFile ($fileCount files)"

# ── 5. WiX build ─────────────────────────────────────────────────────────────
Write-Host "`n[5/5] Building MSI with WiX v7…" -ForegroundColor Yellow
$MsiOut = Join-Path $OutputDir "TimeTracker.msi"

wix build `
    -d "Version=$Version" `
    -d "DistDir=$DistDir" `
    -ext WixToolset.UI.wixext `
    -ext WixToolset.Util.wixext `
    -out $MsiOut `
    $WxsFile $FragFile

if ($LASTEXITCODE -ne 0) { throw "wix build failed" }

# ── Done ──────────────────────────────────────────────────────────────────────
$size = [math]::Round((Get-Item $MsiOut).Length / 1MB, 1)
Write-Host "`n=== Build complete ===" -ForegroundColor Green
Write-Host "MSI     : $MsiOut ($size MB)"
Write-Host "Version : $Version"
Write-Host "API Base: $ApiBase"
