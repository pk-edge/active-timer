# TimeTracker Windows Build Script
#
# Builds timetracker.exe once with PyInstaller, then packages a single MSI.
# backends.json is the single source of truth for URL + token.
#
# Usage:
#   cd active-time\installer\windows
#   .\build_windows.ps1                              — uses backends.json from agent dir if present
#   .\build_windows.ps1 <token> [<api_url>]          — embeds given token and optional API URL
#   .\build_windows.ps1 -SkipPyInstaller             — re-package existing dist\
#
# Prerequisites (install once):
#   1. Python 3.11 or 3.12 (python.org) — add to PATH
#   2. .NET SDK: https://dotnet.microsoft.com
#   3. WiX v7:
#        dotnet tool install --global wix
#        wix eula accept wix7
#        wix extension add WixToolset.UI.wixext/7.0.0 -g
#        wix extension add WixToolset.Util.wixext/7.0.0 -g
#
# Output: output\TimeTracker-<version>.msi

param(
    [Parameter(Position=0)][string]$Token          = "",
    [Parameter(Position=1)][string]$ApiUrl         = "",
    [string]$Version        = "",        # auto-detected from version.py if blank
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentDir   = (Resolve-Path (Join-Path $ScriptDir "..\..\agent")).Path
$DistDir    = Join-Path $AgentDir  "dist\timetracker"
$OutputDir  = Join-Path $ScriptDir "output"
$WxsFile    = Join-Path $ScriptDir "TimeTracker.wxs"
$WixObjDir  = Join-Path $ScriptDir "wix_obj"
$FragFile   = Join-Path $WixObjDir "AgentFiles.wxs"

# ── 1. Virtual environment + PyInstaller ──────────────────────────────────────
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

# ── Auto-detect version ───────────────────────────────────────────────────────
if (-not $Version) {
    $venvPython = Join-Path $AgentDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        Push-Location $AgentDir
        $Version = (& $venvPython -c "from version import __version__; print(__version__)" 2>$null)
        Pop-Location
    }
    if (-not $Version) { $Version = "1.0.0" }
}

Write-Host "Building TimeTracker v$Version …"

# ── 2. Embed backends.json ────────────────────────────────────────────────────
# backends.json is the single source of truth for URL + token.
# Written into _internal\ so config.py picks it up as bundle resources.
$InternalDir = Join-Path $DistDir "_internal"
New-Item -ItemType Directory -Force -Path $InternalDir | Out-Null

if ($Token -and $ApiUrl) {
    $json = '[{"url":"' + $ApiUrl + '","token":"' + $Token + '"}]'
    Set-Content -Path (Join-Path $InternalDir "backends.json") -Value $json -Encoding UTF8
    Write-Host "Embedded backends.json from arguments (url=$ApiUrl)."
} elseif ($Token) {
    $srcBackends = Join-Path $AgentDir "backends.json"
    if (Test-Path $srcBackends) {
        Copy-Item $srcBackends (Join-Path $InternalDir "backends.json") -Force
        Write-Host "Embedded backends.json (token arg ignored — backends.json has its own token)."
    } else {
        $tokenBytes = [System.Text.Encoding]::UTF8.GetBytes($Token)
        [System.IO.File]::WriteAllBytes((Join-Path $InternalDir "enrollment_token.txt"), $tokenBytes)
        Write-Host "Embedded enrollment_token.txt (no backends.json found)."
    }
} else {
    $srcBackends = Join-Path $AgentDir "backends.json"
    if (Test-Path $srcBackends) {
        Copy-Item $srcBackends (Join-Path $InternalDir "backends.json") -Force
        Write-Host "Embedded backends.json."
    }
}

# ── 3. Generate WiX fragment (all _internal\ files auto-enumerated) ───────────
Write-Host "`n[3/3] Packaging MSI…" -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $WixObjDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir  | Out-Null

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

# ── 4. Build MSI ──────────────────────────────────────────────────────────────
$MsiOut = Join-Path $OutputDir "TimeTracker-${Version}.msi"

wix build `
    -d "Version=$Version" `
    -d "DistDir=$DistDir" `
    -ext WixToolset.UI.wixext `
    -ext WixToolset.Util.wixext `
    -out $MsiOut `
    $WxsFile $FragFile

if ($LASTEXITCODE -ne 0) { throw "wix build failed" }

$size = [math]::Round((Get-Item $MsiOut).Length / 1MB, 1)

Write-Host ""
Write-Host "✓ Built: $MsiOut ($size MB)" -ForegroundColor Green
Write-Host "  Version : $Version"
Write-Host ""
Write-Host "User flow: msiexec /i `"$MsiOut`" /quiet /norestart"
