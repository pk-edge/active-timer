# TimeTracker Windows Build — v1.0.2

## Prerequisites (install once on the Windows build machine)

| Tool | Version | Download |
|---|---|---|
| Python | 3.11 or 3.12 | https://python.org |
| WiX Toolset | 3.11 | https://wixtoolset.org/releases/ |

Python must be on `PATH` (`python --version` works in a new PowerShell window).  
WiX adds `candle.exe`, `light.exe`, `heat.exe` to PATH automatically.

## Build steps

```powershell
# 1. Open PowerShell as your normal user (NOT admin)
# 2. Navigate to this directory
cd path\to\active-time\installer\windows

# 3. Allow script execution (once per machine)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 4. Run the build — defaults to http://10.10.20.77:8000
.\build_windows.ps1

# 5. Override the server IP if needed
.\build_windows.ps1 -ApiBase "http://YOUR_SERVER_IP:8000"
```

## Output

`output\TimeTracker.msi` — double-click to install, or deploy silently:

```powershell
msiexec /i TimeTracker.msi /quiet /norestart
```

## What the MSI does

- Installs to `%LocalAppData%\TimeTracker\`
- Registers a `Run` key so the agent starts automatically at login
- Creates a Start Menu shortcut
- Launches the agent immediately after install
- Uninstall via `Control Panel → Programs` or `msiexec /x TimeTracker.msi`

## Changing the server URL after install

Edit `%LocalAppData%\TimeTracker\_internal\api_base.txt` and restart the agent.
