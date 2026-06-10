# TimeTracker Windows Build

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

# 4. Build — embeds backends.json from agent dir if present
.\build_windows.ps1

# 5. Embed a specific token + URL at build time
.\build_windows.ps1 <enrollment_token> https://api.tracker.onedge.co

# 6. Token only (URL comes from existing backends.json)
.\build_windows.ps1 <enrollment_token>

# 7. Skip PyInstaller if already built (faster re-package)
.\build_windows.ps1 -SkipPyInstaller
```

## Output

`output\TimeTracker-<version>.msi` — install interactively or silently:

```powershell
# Silent install
msiexec /i "output\TimeTracker-1.1.3.msi" /quiet /norestart

# Silent uninstall
msiexec /x "output\TimeTracker-1.1.3.msi" /quiet /norestart
```

## What the MSI does

- Kills any running TimeTracker instance before upgrading
- Clears `%APPDATA%\TimeTracker\identity.json` so the agent re-enrolls fresh against the backend in `backends.json`
- Installs to `%LocalAppData%\TimeTracker\`
- Embeds `backends.json` in `_internal\` (single source of truth for URL + token)
- Registers a `HKCU Run` key so the agent starts automatically at login
- Creates a Start Menu shortcut under **TimeTracker**
- Launches the agent immediately after install completes
- Uninstall via **Control Panel → Programs** or the command above

## Changing the backend after install

Edit `%LocalAppData%\TimeTracker\_internal\backends.json` and restart the agent:

```powershell
notepad "$env:LOCALAPPDATA\TimeTracker\_internal\backends.json"

# Restart
Stop-Process -Name timetracker -Force -ErrorAction SilentlyContinue
Start-Process "$env:LOCALAPPDATA\TimeTracker\timetracker.exe"
```

## backends.json format

```json
[{"url": "https://api.tracker.onedge.co", "token": "your-enrollment-token"}]
```

Place this file in `active-time\agent\` before running `build_windows.ps1` and it will be embedded automatically.
