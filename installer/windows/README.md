# TimeTracker Windows Build — v1.1.3

## Prerequisites (install once on the Windows build machine)

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | https://python.org — tick **Add to PATH** |
| .NET SDK | 8+ | https://dotnet.microsoft.com |
| WiX Toolset v7 | 7.x | `dotnet tool install --global wix` |

### WiX first-time setup

```powershell
# Accept license
wix eula accept wix7

# Add required extensions (once per machine)
wix extension add WixToolset.UI.wixext/7.0.0 -g
wix extension add WixToolset.Util.wixext/7.0.0 -g
```

## Build steps

```powershell
# 1. Open PowerShell (normal user, NOT admin)
# 2. Allow scripts (once per machine)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 3. Navigate to this directory
cd path\to\active-time\installer\windows

# 4. Build — defaults to production API (10.10.21.230:8000)
.\build_windows.ps1

# 5. Override server URL if needed
.\build_windows.ps1 -ApiBase "https://api.tracker.onedge.co"

# 6. Skip PyInstaller if already built (faster re-package)
.\build_windows.ps1 -SkipPyInstaller
```

## Output

`output\TimeTracker.msi` — install interactively or silently:

```powershell
# Silent install
msiexec /i output\TimeTracker.msi /quiet /norestart

# Silent uninstall
msiexec /x output\TimeTracker.msi /quiet /norestart
```

## What the MSI does

- Installs to `%LocalAppData%\TimeTracker\`
- Registers a `HKCU Run` key so the agent starts automatically at login
- Creates a Start Menu shortcut under **TimeTracker**
- Launches the agent immediately after install completes
- Uninstall via **Control Panel → Programs** or the command above

## Changing the server URL after install

Edit `%LocalAppData%\TimeTracker\_internal\api_base.txt` and restart the agent:

```powershell
# Find and edit the file
notepad "$env:LOCALAPPDATA\TimeTracker\_internal\api_base.txt"

# Restart the agent (kills the running process, autostart picks it up)
Stop-Process -Name timetracker -Force -ErrorAction SilentlyContinue
Start-Process "$env:LOCALAPPDATA\TimeTracker\timetracker.exe"
```

## Changing the API base at build time

```powershell
.\build_windows.ps1 -ApiBase "http://10.10.20.77:8000"
```

The script writes the value into `_internal\api_base.txt` inside the built dist folder before packaging it into the MSI.
