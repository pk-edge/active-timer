; TimeTracker — Inno Setup script
; Free, no .NET SDK required.
;
; Built by build_exe.ps1 — do not run iscc directly (token/api_base must
; be written into dist\_internal\ first by the PowerShell script).

[Setup]
AppName=TimeTracker
AppVersion=1.0.0
AppPublisher=EDGE
DefaultDirName={localappdata}\Programs\TimeTracker
OutputBaseFilename=TimeTracker-Setup
OutputDir=output
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
ShowLanguageDialog=no
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
; Entire PyInstaller bundle (exe + _internal\ with token + api_base baked in)
Source: "..\..\agent\dist\timetracker\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Copy token to %APPDATA%\TimeTracker\ so the agent finds it on first run
Source: "..\..\agent\dist\timetracker\_internal\enrollment_token.txt"; DestDir: "{userappdata}\TimeTracker"; Flags: ignoreversion uninsneveruninstall

[Registry]
; Auto-start on every Windows login (per-user, no admin needed)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "TimeTracker"; \
  ValueData: """{app}\timetracker.exe"""; Flags: uninsdeletevalue

[Run]
; Launch immediately after install
Filename: "{app}\timetracker.exe"; Flags: nowait postinstall

[UninstallRun]
; Kill the running agent before files are removed
Filename: "taskkill"; Parameters: "/f /im timetracker.exe"; \
  Flags: runhidden waituntilterminated; RunOnceId: "KillApp"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
