# TimeTracker — Deployment Guide

## Architecture

```
Laptops (Windows / macOS)
  └─ Agent (self-contained .exe / .app)
       ├─ tracks active window, idle time, URLs
       ├─ queues events locally in SQLite (works offline)
       └─ syncs to ──► Backend API (FastAPI)
                              └─ PostgreSQL + S3 (MinIO)
                       ◄──── Admin Dashboard (Next.js)
```

---

## Step 1 — Start the backend

```bash
cp .env.example .env          # fill in POSTGRES_PASSWORD and JWT_SECRET
docker compose up --build -d
```

Services started:
| Service | URL |
|---|---|
| Backend API | http://localhost:8000 |
| Dashboard | http://localhost:3002 |
| MinIO console | http://localhost:9001 |

---

## Step 2 — Create admin account and get enrollment token

```bash
docker compose exec backend python -m app.scripts.seedAdmin \
    --company "Your Company" \
    --email   admin@yourcompany.com \
    --name    "Admin"
# You will be prompted for a password.
# The command prints the Company Enrollment Token — copy it.
```

The enrollment token is what links each laptop to your company in the backend.

---

## Step 3 — Build the installer

### macOS → `TimeTracker-<version>.dmg`

**Build machine needs:** macOS with Xcode command-line tools, and `agent/.venv`
created from the **python.org universal2** Python (Homebrew/pyenv builds are
single-arch and the build refuses them):

```bash
cd agent
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

```bash
cd installer/macos
./build_macos.sh                          # token + URL from agent/backends.json
./build_macos.sh "<token>" "<api_url>"    # or embed explicit values
```

Output: `installer/macos/TimeTracker-<version>.dmg` (~80 MB, containing a `.pkg`).
One build runs natively on every supported Mac — Apple Silicon and Intel,
macOS 12 Monterey or later (the build fails if any bundled binary is not
universal2).

---

### Windows → `TimeTracker.msi`

**Must be built on a Windows machine.** (PyInstaller only bundles for the OS it runs on.)

**Build machine needs:** Python 3.11+, .NET 6+ Runtime (for WiX — installed automatically)

```powershell
cd installer\windows
.\build_msi.ps1 -CompanyToken "paste-your-token-here" -ApiBase "http://localhost:8000"
```

Or using the batch wrapper:
```cmd
build_msi.bat paste-your-token-here http://localhost:8000
```

Output: `installer\windows\output\TimeTracker.msi` (~180 MB, fully self-contained)

WiX Toolset v4 is downloaded and installed automatically via `dotnet tool install` if not present.

---

## Step 4 — Install on laptops

### macOS

1. Copy `TimeTracker-<version>.dmg` to the laptop (email, USB, shared drive — any way)
2. Open the DMG → double-click the `.pkg` → **Install** — installs for the current
   user into `~/Applications`, **no admin password needed**
3. The installer starts the agent immediately under launchd — no manual launch
4. **Auto-starts on every login** and is restarted automatically if it ever crashes
   (LaunchAgent `com.timetracker.agent`)
5. **Updates are silent**: upload the new `.dmg` on the dashboard's Agents page and
   click **Activate** (mark it *mandatory* to roll out within one check). The
   version you type must equal `agent/version.py` of that build.

### Windows

1. Copy `TimeTracker.msi` to the laptop
2. Double-click it — installs silently with just a progress bar, then closes
3. The floating timer appears in the top-right corner
4. **Auto-starts on every Windows login automatically** (registry Run key set by installer)

**Silent IT/MDM deploy:**
```cmd
msiexec /i TimeTracker.msi /quiet /qn /norestart
```

---

## Rebuilding with a different token or API URL

Just re-run the build command with the new values. The token and API URL are baked
into the installer at build time — no configuration needed on the target laptop.

```bash
# macOS — rebuild
./build_macos.sh "new-token" "https://api.prod.example.com"

# Windows — rebuild (re-use existing PyInstaller output)
.\build_msi.ps1 -CompanyToken "new-token" -ApiBase "https://api.prod.example.com" -SkipPyInstaller
```

---

## Dashboard

Open http://localhost:3002 (or your production URL) and log in with the admin
email and password set in Step 2.

- **Devices** — live grid of all enrolled laptops with online/offline status
- **Device detail** — activity timeline, 7-day chart, screenshots tab
- **Reports** — company-wide totals, top apps, top URLs

---

## Agent behavior reference

| Situation | Behavior |
|---|---|
| Screen locked | Capture pauses, timer stops |
| Screen unlocked | Capture resumes, timer continues |
| No network | Events queue locally; syncs when back online (exponential backoff, max 5 min) |
| 401 from server | Agent re-enrolls automatically |
| Widget closed | Agent keeps running — restore from system tray icon |
| Tray → Quit | Agent stops gracefully |

---

## Data directories

| OS | Path |
|---|---|
| Windows | `%APPDATA%\TimeTracker\` |
| macOS | `~/Library/Application Support/TimeTracker/` |

Files: `queue.db` (event queue), `identity.json` (device ID + JWT), `agent.log`

---

## Troubleshooting

**Timer widget doesn't appear**
Check the agent log for errors. The most common causes:
- Enrollment token is wrong or backend is unreachable → check `TT_API_BASE`
- Token file missing → check `enrollment_token.txt` exists in the data directory

**Agent not starting on login**
- Windows: `regedit` → `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → confirm `TimeTracker` key exists
- macOS: `launchctl list | grep timetracker` → should return a line with `com.timetracker.agent`

**Re-enroll a device** (e.g. after reinstall or token change)
Delete `identity.json` from the data directory and restart the agent.
The agent will re-enroll automatically on next launch.
