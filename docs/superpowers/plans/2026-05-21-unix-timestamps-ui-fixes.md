# Unix Timestamps + UI Fixes + Windows MSI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ISO 8601 string timestamps with Unix integer seconds across the entire stack (agent → backend → dashboard), fix all timezone-related UI bugs, and ensure Windows MSI writes api_base.txt.

**Architecture:** Agent stores unix seconds in SQLite (one-time TEXT→INTEGER migration gated by `PRAGMA user_version`), sends integers to backend which stores them as TIMESTAMPTZ (no Postgres change), returns `*_unix` integer fields alongside existing ISO fields. Dashboard converts with `new Date(ts * 1000).toLocaleTimeString()` so the browser handles timezone automatically.

**Tech Stack:** Python 3.11 (agent/SQLite), FastAPI + SQLAlchemy async + PostgreSQL (backend), Next.js 14 + TypeScript + SWR (dashboard), PowerShell + WiX v4 (installer)

---

## File Map

| File | Change |
|------|--------|
| `agent/db/local_db.py` | SCHEMA INTEGER timestamps; migration fn; `int(time.time())` inserts |
| `backend/app/schemas/event.py` | `timestamp: Union[int, str]` |
| `backend/app/schemas/device.py` | `timestamp_unix: int` on TimelineEventOut; `last_seen_unix: Optional[int]` on DeviceOut |
| `backend/app/schemas/screenshot.py` | `captured_at: Union[int, str]` on UploadRequest; `captured_at_unix: int` on ScreenshotOut |
| `backend/app/api/v1/endpoints/ingest.py` | Handle int or str timestamp |
| `backend/app/api/v1/endpoints/logs.py` | `since`/`until` unix params; `timestamp_unix` in response; remove ±1 day hack |
| `backend/app/api/v1/endpoints/devices.py` | `since`/`until` for timeline; `last_seen_unix` in list; `timestamp_unix` in timeline |
| `backend/app/api/v1/endpoints/screenshots.py` | Handle int `captured_at`; `captured_at_unix` in list response |
| `dashboard/src/types/index.ts` | Add `*_unix` fields to all interfaces |
| `dashboard/src/lib/api.ts` | `fmtTime(ts: number)`; `getLogs`/`getTimeline` with `since`/`until` |
| `dashboard/src/app/(dashboard)/logs/page.tsx` | Date → unix conversion for API call; use `timestamp_unix` |
| `dashboard/src/app/(dashboard)/devices/[id]/page.tsx` | Init `selDate` to `""`; date → unix for timeline |
| `dashboard/src/app/(dashboard)/devices/_components/DeviceCard.tsx` | Use `last_seen_unix` |
| `dashboard/src/app/(dashboard)/devices/[id]/_components/ActivityTimeline.tsx` | Use `timestamp_unix` |
| `dashboard/src/app/(dashboard)/devices/[id]/_components/ScreenshotGrid.tsx` | Use `captured_at_unix` |
| `dashboard/src/app/(dashboard)/devices/[id]/_components/DailyChart.tsx` | Remove `T12:00:00` DST hack |
| `installer/windows/build_msi.ps1` | Write `_internal\api_base.txt` after PyInstaller |

---

### Task 1: Agent — SQLite schema + migration

**Files:**
- Modify: `agent/db/local_db.py`

- [ ] **Step 1: Update imports — replace datetime with time**

Replace:
```python
from datetime import datetime, timezone
```
With:
```python
import time
```

- [ ] **Step 2: Update SCHEMA constant to use INTEGER timestamp columns**

Replace the entire `SCHEMA` string:
```python
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    app_name     TEXT,
    window_title TEXT,
    url          TEXT,
    duration     INTEGER DEFAULT 0,
    ts           INTEGER NOT NULL,
    synced       INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_events_synced ON events(synced);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);

CREATE TABLE IF NOT EXISTS screenshots (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    local_path  TEXT NOT NULL,
    s3_key      TEXT,
    captured_at INTEGER NOT NULL,
    uploaded    INTEGER DEFAULT 0,
    file_size   INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_screenshots_uploaded ON screenshots(uploaded);

CREATE TABLE IF NOT EXISTS attendance_sessions (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    active_secs INTEGER DEFAULT 0,
    synced      INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_synced ON attendance_sessions(synced);
"""
```

- [ ] **Step 3: Add _migrate method to LocalDB class (insert before init_schema)**

```python
def _migrate(self, conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return
    cols = {c[1]: c[2].upper() for c in conn.execute("PRAGMA table_info(events)").fetchall()}
    if cols.get("ts") != "TEXT":
        # Fresh install — tables already created with INTEGER schema
        conn.execute("PRAGMA user_version = 2")
        return
    # Existing install — migrate TEXT → INTEGER via table reconstruction
    conn.executescript("""
        DROP TABLE IF EXISTS _events_new;
        CREATE TABLE _events_new (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, event_type TEXT NOT NULL,
            app_name TEXT, window_title TEXT, url TEXT, duration INTEGER DEFAULT 0,
            ts INTEGER NOT NULL, synced INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        INSERT INTO _events_new SELECT id,device_id,event_type,app_name,window_title,url,duration,
            COALESCE(CAST(strftime('%s',ts) AS INTEGER),0),synced,created_at FROM events;
        DROP TABLE events;
        ALTER TABLE _events_new RENAME TO events;
        CREATE INDEX IF NOT EXISTS idx_events_synced ON events(synced);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

        DROP TABLE IF EXISTS _screenshots_new;
        CREATE TABLE _screenshots_new (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, local_path TEXT NOT NULL,
            s3_key TEXT, captured_at INTEGER NOT NULL, uploaded INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        INSERT INTO _screenshots_new SELECT id,device_id,local_path,s3_key,
            COALESCE(CAST(strftime('%s',captured_at) AS INTEGER),0),uploaded,file_size,created_at FROM screenshots;
        DROP TABLE screenshots;
        ALTER TABLE _screenshots_new RENAME TO screenshots;
        CREATE INDEX IF NOT EXISTS idx_screenshots_uploaded ON screenshots(uploaded);

        DROP TABLE IF EXISTS _sessions_new;
        CREATE TABLE _sessions_new (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, started_at INTEGER NOT NULL,
            ended_at INTEGER, active_secs INTEGER DEFAULT 0, synced INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        INSERT INTO _sessions_new SELECT id,device_id,
            COALESCE(CAST(strftime('%s',started_at) AS INTEGER),0),
            CASE WHEN ended_at IS NOT NULL THEN CAST(strftime('%s',ended_at) AS INTEGER) ELSE NULL END,
            active_secs,synced,created_at FROM attendance_sessions;
        DROP TABLE attendance_sessions;
        ALTER TABLE _sessions_new RENAME TO attendance_sessions;
        CREATE INDEX IF NOT EXISTS idx_sessions_synced ON attendance_sessions(synced);

        PRAGMA user_version = 2;
    """)
    log.info("LocalDB migrated to v2 (unix integer timestamps)")
```

- [ ] **Step 4: Call _migrate inside init_schema**

Replace:
```python
def init_schema(self) -> None:
    with self._conn() as conn:
        conn.executescript(SCHEMA)
    log.info("LocalDB initialized at %s", self.db_path)
```
With:
```python
def init_schema(self) -> None:
    with self._conn() as conn:
        conn.executescript(SCHEMA)
        self._migrate(conn)
    log.info("LocalDB initialized at %s", self.db_path)
```

- [ ] **Step 5: Update insert_event to use int(time.time())**

Replace:
```python
    def insert_event(self, event: dict) -> str:
        if not self._device_id:
            log.debug("insert_event skipped — not yet enrolled")
            return ""

        row_id = str(uuid.uuid4())
        ts     = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO events
                   (id, device_id, event_type, app_name, window_title, url, duration, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    self._device_id,
                    event.get("type", "unknown"),
                    event.get("app"),
                    event.get("title"),
                    event.get("url"),
                    event.get("duration", 0),
                    ts,
                ),
            )
        return row_id
```
With:
```python
    def insert_event(self, event: dict) -> str:
        if not self._device_id:
            log.debug("insert_event skipped — not yet enrolled")
            return ""

        row_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO events
                   (id, device_id, event_type, app_name, window_title, url, duration, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row_id,
                    self._device_id,
                    event.get("type", "unknown"),
                    event.get("app"),
                    event.get("title"),
                    event.get("url"),
                    event.get("duration", 0),
                    int(time.time()),
                ),
            )
        return row_id
```

- [ ] **Step 6: Update insert_screenshot to use int(time.time())**

Replace:
```python
    def insert_screenshot(self, local_path: str, file_size: int) -> str:
        if not self._device_id:
            log.debug("insert_screenshot skipped — not yet enrolled")
            return ""
        row_id = str(uuid.uuid4())
        ts     = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO screenshots
                   (id, device_id, local_path, captured_at, file_size)
                   VALUES (?, ?, ?, ?, ?)""",
                (row_id, self._device_id, local_path, ts, file_size),
            )
        return row_id
```
With:
```python
    def insert_screenshot(self, local_path: str, file_size: int) -> str:
        if not self._device_id:
            log.debug("insert_screenshot skipped — not yet enrolled")
            return ""
        row_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO screenshots
                   (id, device_id, local_path, captured_at, file_size)
                   VALUES (?, ?, ?, ?, ?)""",
                (row_id, self._device_id, local_path, int(time.time()), file_size),
            )
        return row_id
```

- [ ] **Step 7: Update start_session to use int(time.time())**

Replace:
```python
    def start_session(self) -> str:
        if not self._device_id:
            log.debug("start_session skipped — not yet enrolled")
            return ""
        session_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO attendance_sessions (id, device_id, started_at) VALUES (?, ?, ?)",
                (session_id, self._device_id, ts),
            )
        log.info("Attendance session started: %s", session_id)
        return session_id
```
With:
```python
    def start_session(self) -> str:
        if not self._device_id:
            log.debug("start_session skipped — not yet enrolled")
            return ""
        session_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO attendance_sessions (id, device_id, started_at) VALUES (?, ?, ?)",
                (session_id, self._device_id, int(time.time())),
            )
        log.info("Attendance session started: %s", session_id)
        return session_id
```

- [ ] **Step 8: Update end_session to use int(time.time())**

Replace:
```python
    def end_session(self, session_id: str, active_secs: int) -> None:
        if not session_id:
            return
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE attendance_sessions SET ended_at=?, active_secs=? WHERE id=?",
                (ts, active_secs, session_id),
            )
        log.info("Attendance session ended: %s (%ds active)", session_id, active_secs)
```
With:
```python
    def end_session(self, session_id: str, active_secs: int) -> None:
        if not session_id:
            return
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE attendance_sessions SET ended_at=?, active_secs=? WHERE id=?",
                (int(time.time()), active_secs, session_id),
            )
        log.info("Attendance session ended: %s (%ds active)", session_id, active_secs)
```

- [ ] **Step 9: Verify with a quick Python test**

```bash
cd /Users/aghaammarkhan/EDGE/timetracker/agent
python3 - <<'EOF'
import tempfile, os
from pathlib import Path
from db.local_db import LocalDB

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    path = f.name
db = LocalDB(Path(path))
db.init_schema()
db.set_device_id("test-device")
db.insert_event({"type": "window_change", "app": "TestApp"})
events = db.get_unsynced_events()
assert isinstance(events[0]["ts"], int), f"ts should be int, got {type(events[0]['ts'])}"
print("OK - ts is int:", events[0]["ts"])
os.unlink(path)
EOF
```
Expected output: `OK - ts is int: <unix seconds integer>`

- [ ] **Step 10: Commit**

```bash
git add agent/db/local_db.py
git commit -m "feat(agent): store unix integer timestamps in SQLite; one-time TEXT→INTEGER migration on startup"
```

---

### Task 2: Backend schemas

**Files:**
- Modify: `backend/app/schemas/event.py`
- Modify: `backend/app/schemas/device.py`
- Modify: `backend/app/schemas/screenshot.py`

- [ ] **Step 1: Add Union to event.py and change timestamp type**

In `backend/app/schemas/event.py`, replace:
```python
from typing import Optional, List
```
With:
```python
from typing import Optional, List, Union
```

Replace:
```python
    timestamp:    str
```
With:
```python
    timestamp:    Union[int, str]
```

- [ ] **Step 2: Add *_unix fields to device.py**

Replace the entire `backend/app/schemas/device.py`:
```python
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DeviceOut(BaseModel):
    id:             str
    hostname:       str
    platform:       str
    employee_name:  Optional[str]
    last_seen:      Optional[datetime]
    last_seen_unix: Optional[int]
    is_online:      bool

    model_config = {"from_attributes": True}


class TimelineEventOut(BaseModel):
    id:             str
    event_type:     str
    app_name:       Optional[str]
    window_title:   Optional[str]
    url:            Optional[str]
    duration:       int
    timestamp:      datetime
    timestamp_unix: int


class DailyStatOut(BaseModel):
    date:           str
    active_seconds: int
    idle_seconds:   int
    total_seconds:  int
```

- [ ] **Step 3: Update screenshot.py**

Read `backend/app/schemas/screenshot.py` to see current content, then apply these changes:
- Add `Union` to typing imports
- Change `captured_at: str` → `captured_at: Union[int, str]` on `UploadRequest`
- Add `captured_at_unix: int` to `ScreenshotOut`

The final `backend/app/schemas/screenshot.py` should be:
```python
from datetime import datetime, timezone
from typing import Optional, Union
from pydantic import BaseModel


class UploadRequest(BaseModel):
    screenshot_id: str
    captured_at:   Union[int, str]
    file_size:     int


class UploadResponse(BaseModel):
    upload_url: str
    s3_key:     str


class ScreenshotOut(BaseModel):
    id:               str
    s3_url:           str
    captured_at:      datetime
    captured_at_unix: int
    file_size:        int
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/schemas/event.py backend/app/schemas/device.py backend/app/schemas/screenshot.py
git commit -m "feat(backend): union timestamp types and *_unix fields in schemas"
```

---

### Task 3: Backend ingest.py — handle int or str timestamp

**Files:**
- Modify: `backend/app/api/v1/endpoints/ingest.py`

- [ ] **Step 1: Add timezone to datetime import**

Replace:
```python
from datetime import datetime
```
With:
```python
from datetime import datetime, timezone
```

- [ ] **Step 2: Update timestamp parse block**

Replace:
```python
        try:
            ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
```
With:
```python
        try:
            if isinstance(e.timestamp, int):
                ts = datetime.fromtimestamp(e.timestamp, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
        except (ValueError, OSError):
            continue
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/v1/endpoints/ingest.py
git commit -m "feat(backend): accept integer unix timestamp in ingest endpoint"
```

---

### Task 4: Backend screenshots.py — int captured_at + captured_at_unix in list

**Files:**
- Modify: `backend/app/api/v1/endpoints/screenshots.py`

- [ ] **Step 1: Update captured_at parse in request_upload_url**

Replace:
```python
    try:
        captured = datetime.fromisoformat(body.captured_at.replace("Z", "+00:00"))
    except ValueError:
        captured = datetime.now(timezone.utc)
```
With:
```python
    try:
        if isinstance(body.captured_at, int):
            captured = datetime.fromtimestamp(body.captured_at, tz=timezone.utc)
        else:
            captured = datetime.fromisoformat(body.captured_at.replace("Z", "+00:00"))
    except (ValueError, OSError):
        captured = datetime.now(timezone.utc)
```

- [ ] **Step 2: Add captured_at_unix to list_screenshots response**

Replace:
```python
        out.append(ScreenshotOut(id=str(ss.id), s3_url=url, captured_at=ss.captured_at, file_size=ss.file_size))
```
With:
```python
        out.append(ScreenshotOut(
            id=str(ss.id),
            s3_url=url,
            captured_at=ss.captured_at,
            captured_at_unix=int(ss.captured_at.timestamp()),
            file_size=ss.file_size,
        ))
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/v1/endpoints/screenshots.py
git commit -m "feat(backend): handle int captured_at; add captured_at_unix to screenshot list response"
```

---

### Task 5: Backend logs.py — since/until unix params

**Files:**
- Modify: `backend/app/api/v1/endpoints/logs.py`

- [ ] **Step 1: Replace entire file**

```python
"""
GET /v1/logs — company-wide activity feed for the dashboard Logs page.
Returns the most recent events across all devices belonging to the admin's company.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import get_db, get_current_admin
from app.db.models import Employee

router = APIRouter()


@router.get("", response_model=list[dict])
async def get_logs(
    since:      Optional[int] = Query(default=None),
    until:      Optional[int] = Query(default=None),
    device_id:  Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    limit:      int = Query(default=200, le=1000),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    params: dict = {"company_id": str(admin.company_id), "limit": limit}
    where_clauses = ["d.company_id = :company_id", "d.is_active = TRUE"]

    if since is not None:
        where_clauses.append("ae.timestamp >= to_timestamp(:since)")
        params["since"] = since
    else:
        default_since = datetime.now(timezone.utc) - timedelta(hours=48)
        where_clauses.append("ae.timestamp >= :default_since")
        params["default_since"] = default_since

    if until is not None:
        where_clauses.append("ae.timestamp < to_timestamp(:until)")
        params["until"] = until

    if device_id:
        where_clauses.append("d.id = :device_id::uuid")
        params["device_id"] = device_id

    if event_type:
        where_clauses.append("ae.event_type = :event_type")
        params["event_type"] = event_type

    where_sql = " AND ".join(where_clauses)

    result = await db.execute(
        text(f"""
            SELECT
                ae.id, ae.event_type, ae.app_name, ae.window_title,
                ae.url, ae.duration, ae.timestamp,
                EXTRACT(EPOCH FROM ae.timestamp)::bigint AS timestamp_unix,
                d.id       AS device_id,
                d.hostname AS hostname
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE {where_sql}
            ORDER BY ae.timestamp DESC
            LIMIT :limit
        """),
        params,
    )

    rows = result.fetchall()
    return [
        {
            "id":             str(r.id),
            "event_type":     r.event_type,
            "app_name":       r.app_name,
            "window_title":   r.window_title,
            "url":            r.url,
            "duration":       r.duration,
            "timestamp":      r.timestamp.isoformat() if r.timestamp else None,
            "timestamp_unix": r.timestamp_unix,
            "device_id":      str(r.device_id),
            "hostname":       r.hostname,
        }
        for r in rows
    ]
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/api/v1/endpoints/logs.py
git commit -m "feat(backend): replace date filter with since/until unix params; add timestamp_unix to logs response"
```

---

### Task 6: Backend devices.py — last_seen_unix + since/until timeline

**Files:**
- Modify: `backend/app/api/v1/endpoints/devices.py`

- [ ] **Step 1: Remove unused date import**

Replace:
```python
from datetime import datetime, date, timedelta, timezone
```
With:
```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 2: Add last_seen_unix to list_devices return**

Replace:
```python
    return [
        DeviceOut(
            id=str(d.id),
            hostname=d.hostname,
            platform=d.platform,
            employee_name=d.employee.name if d.employee else None,
            last_seen=d.last_seen,
            is_online=bool(d.last_seen and d.last_seen > cutoff),
        )
        for d in devices
    ]
```
With:
```python
    return [
        DeviceOut(
            id=str(d.id),
            hostname=d.hostname,
            platform=d.platform,
            employee_name=d.employee.name if d.employee else None,
            last_seen=d.last_seen,
            last_seen_unix=int(d.last_seen.timestamp()) if d.last_seen else None,
            is_online=bool(d.last_seen and d.last_seen > cutoff),
        )
        for d in devices
    ]
```

- [ ] **Step 3: Replace get_timeline with since/until version**

Replace the entire `get_timeline` function:
```python
@router.get("/{device_id}/timeline", response_model=List[TimelineEventOut])
async def get_timeline(
    device_id: str,
    since:     Optional[int] = Query(default=None),
    until:     Optional[int] = Query(default=None),
    limit:     int = Query(default=500, le=2000),
    offset:    int = Query(default=0, ge=0),
    admin: Employee = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_for_admin(device_id, admin, db)

    q = select(ActivityEvent).where(ActivityEvent.device_id == device.id)
    if since is not None:
        q = q.where(ActivityEvent.timestamp >= datetime.fromtimestamp(since, tz=timezone.utc))
    if until is not None:
        q = q.where(ActivityEvent.timestamp < datetime.fromtimestamp(until, tz=timezone.utc))

    result = await db.execute(
        q.order_by(ActivityEvent.timestamp.asc()).offset(offset).limit(limit)
    )
    return [
        TimelineEventOut(
            id=str(e.id), event_type=e.event_type,
            app_name=e.app_name, window_title=e.window_title,
            url=e.url, duration=e.duration, timestamp=e.timestamp,
            timestamp_unix=int(e.timestamp.timestamp()),
        )
        for e in result.scalars().all()
    ]
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/v1/endpoints/devices.py
git commit -m "feat(backend): last_seen_unix in device list; since/until unix params and timestamp_unix for timeline"
```

---

### Task 7: Dashboard types/index.ts

**Files:**
- Modify: `dashboard/src/types/index.ts`

- [ ] **Step 1: Add *_unix fields to all interfaces**

Replace the entire file:
```typescript
export interface Device {
  id:             string;
  hostname:       string;
  platform:       string;
  employee_name:  string | null;
  last_seen:      string | null;
  last_seen_unix: number | null;
  is_online:      boolean;
}

export interface TimelineEvent {
  id:             string;
  event_type:     string;
  app_name:       string | null;
  window_title:   string | null;
  url:            string | null;
  duration:       number;
  timestamp:      string;
  timestamp_unix: number;
}

export interface DailyStat {
  date:           string;
  active_seconds: number;
  idle_seconds:   number;
  total_seconds:  number;
}

export interface Screenshot {
  id:               string;
  s3_url:           string;
  captured_at:      string;
  captured_at_unix: number;
  file_size:        number;
}

export interface CompanyDay {
  date:           string;
  active_seconds: number;
  idle_seconds:   number;
  device_count:   number;
}

export interface AppUsage {
  app_name: string;
  seconds:  number;
}

export interface LogEvent {
  id:             string;
  event_type:     string;
  app_name:       string | null;
  window_title:   string | null;
  url:            string | null;
  duration:       number;
  timestamp:      string;
  timestamp_unix: number;
  device_id:      string;
  hostname:       string;
}
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/src/types/index.ts
git commit -m "feat(dashboard): add *_unix integer fields to TypeScript interfaces"
```

---

### Task 8: Dashboard api.ts — fmtTime + since/until

**Files:**
- Modify: `dashboard/src/lib/api.ts`

- [ ] **Step 1: Update fmtTime signature to accept unix seconds**

Replace:
```typescript
export function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
```
With:
```typescript
export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
```

- [ ] **Step 2: Update getTimeline to use since/until**

Replace:
```typescript
export const getTimeline = (deviceId: string, date?: string) =>
  request<TimelineEvent[]>(`/v1/devices/${deviceId}/timeline${date ? `?date=${date}` : ""}`);
```
With:
```typescript
export const getTimeline = (deviceId: string, since?: number, until?: number) => {
  const params = new URLSearchParams();
  if (since !== undefined) params.set("since", String(since));
  if (until !== undefined) params.set("until", String(until));
  const qs = params.toString();
  return request<TimelineEvent[]>(`/v1/devices/${deviceId}/timeline${qs ? `?${qs}` : ""}`);
};
```

- [ ] **Step 3: Update getLogs to use since/until**

Replace:
```typescript
export const getLogs = (opts?: { date?: string; device_id?: string; event_type?: string }) => {
  const params = new URLSearchParams();
  if (opts?.date)       params.set("date",       opts.date);
  if (opts?.device_id)  params.set("device_id",  opts.device_id);
  if (opts?.event_type) params.set("event_type", opts.event_type);
  const qs = params.toString();
  return request<LogEvent[]>(`/v1/logs${qs ? `?${qs}` : ""}`);
};
```
With:
```typescript
export const getLogs = (opts?: { since?: number; until?: number; device_id?: string; event_type?: string }) => {
  const params = new URLSearchParams();
  if (opts?.since !== undefined) params.set("since",      String(opts.since));
  if (opts?.until !== undefined) params.set("until",      String(opts.until));
  if (opts?.device_id)           params.set("device_id",  opts.device_id);
  if (opts?.event_type)          params.set("event_type", opts.event_type);
  const qs = params.toString();
  return request<LogEvent[]>(`/v1/logs${qs ? `?${qs}` : ""}`);
};
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/lib/api.ts
git commit -m "feat(dashboard): fmtTime takes unix seconds; getLogs/getTimeline use since/until params"
```

---

### Task 9: Dashboard logs/page.tsx

**Files:**
- Modify: `dashboard/src/app/(dashboard)/logs/page.tsx`

- [ ] **Step 1: Replace getLogs call with since/until computation**

Replace:
```typescript
  const { data: logs, isLoading, mutate } = useSWR<LogEvent[]>(
    ["logs", selDate, selDevice, selType],
    () => getLogs({ date: selDate, device_id: selDevice || undefined, event_type: selType || undefined }),
    { refreshInterval: 15_000 },
  );
```
With:
```typescript
  const logsOpts = (() => {
    const opts: { since?: number; until?: number; device_id?: string; event_type?: string } = {};
    if (selDate) {
      opts.since = Math.floor(new Date(selDate).getTime() / 1000);
      opts.until = opts.since + 86400;
    }
    if (selDevice) opts.device_id  = selDevice;
    if (selType)   opts.event_type = selType;
    return opts;
  })();

  const { data: logs, isLoading, mutate } = useSWR<LogEvent[]>(
    ["logs", selDate, selDevice, selType],
    () => getLogs(logsOpts),
    { refreshInterval: 15_000 },
  );
```

- [ ] **Step 2: Update fmtTime call in LogRow to use timestamp_unix**

Replace:
```typescript
        {fmtTime(e.timestamp)}
```
With:
```typescript
        {fmtTime(e.timestamp_unix)}
```

- [ ] **Step 3: Commit**

```bash
git add "dashboard/src/app/(dashboard)/logs/page.tsx"
git commit -m "feat(dashboard): logs page converts date to since/until unix; display uses timestamp_unix"
```

---

### Task 10: Dashboard devices/[id]/page.tsx

**Files:**
- Modify: `dashboard/src/app/(dashboard)/devices/[id]/page.tsx`

- [ ] **Step 1: Remove format import (no longer needed)**

Delete the line:
```typescript
import { format } from "date-fns";
```

- [ ] **Step 2: Change selDate initial value and update getTimeline call**

Replace:
```typescript
  const [selDate, setSelDate] = useState(format(new Date(), "yyyy-MM-dd"));

  const { data: events } = useSWR<TimelineEvent[]>(
    ["timeline", params.id, selDate],
    () => getTimeline(params.id, selDate),
    { refreshInterval: 30_000 },
  );
```
With:
```typescript
  const [selDate, setSelDate] = useState("");

  const timelineSince = selDate ? Math.floor(new Date(selDate).getTime() / 1000) : undefined;
  const timelineUntil = selDate ? timelineSince! + 86400 : undefined;

  const { data: events } = useSWR<TimelineEvent[]>(
    ["timeline", params.id, selDate],
    () => getTimeline(params.id, timelineSince, timelineUntil),
    { refreshInterval: 30_000 },
  );
```

- [ ] **Step 3: Commit**

```bash
git add "dashboard/src/app/(dashboard)/devices/[id]/page.tsx"
git commit -m "feat(dashboard): device page init selDate empty; pass since/until unix to getTimeline"
```

---

### Task 11: Dashboard DeviceCard.tsx

**Files:**
- Modify: `dashboard/src/app/(dashboard)/devices/_components/DeviceCard.tsx`

- [ ] **Step 1: Use last_seen_unix instead of last_seen string**

Replace:
```typescript
  const lastSeen = device.last_seen
    ? formatDistanceToNow(new Date(device.last_seen), { addSuffix: true })
    : "never";
```
With:
```typescript
  const lastSeen = device.last_seen_unix
    ? formatDistanceToNow(new Date(device.last_seen_unix * 1000), { addSuffix: true })
    : "never";
```

- [ ] **Step 2: Commit**

```bash
git add "dashboard/src/app/(dashboard)/devices/_components/DeviceCard.tsx"
git commit -m "feat(dashboard): DeviceCard uses last_seen_unix for correct local-time display"
```

---

### Task 12: Dashboard ActivityTimeline.tsx

**Files:**
- Modify: `dashboard/src/app/(dashboard)/devices/[id]/_components/ActivityTimeline.tsx`

- [ ] **Step 1: Use timestamp_unix**

Replace:
```typescript
        <span className="text-xs text-gray-500">{fmtTime(e.timestamp)}</span>
```
With:
```typescript
        <span className="text-xs text-gray-500">{fmtTime(e.timestamp_unix)}</span>
```

- [ ] **Step 2: Commit**

```bash
git add "dashboard/src/app/(dashboard)/devices/[id]/_components/ActivityTimeline.tsx"
git commit -m "feat(dashboard): ActivityTimeline uses timestamp_unix"
```

---

### Task 13: Dashboard ScreenshotGrid.tsx

**Files:**
- Modify: `dashboard/src/app/(dashboard)/devices/[id]/_components/ScreenshotGrid.tsx`

- [ ] **Step 1: Use captured_at_unix**

Replace:
```typescript
            <p className="text-xs text-gray-400">{fmtTime(s.captured_at)}</p>
```
With:
```typescript
            <p className="text-xs text-gray-400">{fmtTime(s.captured_at_unix)}</p>
```

- [ ] **Step 2: Commit**

```bash
git add "dashboard/src/app/(dashboard)/devices/[id]/_components/ScreenshotGrid.tsx"
git commit -m "feat(dashboard): ScreenshotGrid uses captured_at_unix"
```

---

### Task 14: Dashboard DailyChart.tsx — remove T12:00:00 hack

**Files:**
- Modify: `dashboard/src/app/(dashboard)/devices/[id]/_components/DailyChart.tsx`

- [ ] **Step 1: Add parseISO to date-fns import**

Replace:
```typescript
import { format } from "date-fns";
```
With:
```typescript
import { format, parseISO } from "date-fns";
```

- [ ] **Step 2: Remove T12:00:00 append**

Replace:
```typescript
            tickFormatter={d => format(new Date(d + "T12:00:00"), "EEE")}
```
With:
```typescript
            tickFormatter={d => format(parseISO(d), "EEE")}
```

- [ ] **Step 3: Commit**

```bash
git add "dashboard/src/app/(dashboard)/devices/[id]/_components/DailyChart.tsx"
git commit -m "fix(dashboard): DailyChart removes T12:00:00 DST hack; parseISO is safe for date-only strings"
```

---

### Task 15: Windows build_msi.ps1 — write api_base.txt

**Files:**
- Modify: `installer/windows/build_msi.ps1`

- [ ] **Step 1: Add api_base.txt write after the PyInstaller if/else block**

Find this line (it's the end of the PyInstaller else-block and closing brace):
```powershell
    Write-Ok "Bundle ready"
}
```

Add after the closing `}`:
```powershell

# Bake API base into _internal/ so config.py finds it without relying on env var (matches macOS behaviour)
[System.IO.File]::WriteAllText(
    "$DistDir\_internal\api_base.txt",
    $ApiBase,
    (New-Object System.Text.UTF8Encoding $false)
)
Write-Ok "api_base.txt baked into _internal\"
```

This placement means the file is written whether `$SkipPyInstaller` was set or not, keeping it up to date on every build.

- [ ] **Step 2: Commit**

```bash
git add installer/windows/build_msi.ps1
git commit -m "feat(installer): write api_base.txt to _internal/ after PyInstaller — matches macOS bundle behaviour"
```

---

### Task 16: Rebuild + smoke test

**Files:** None — rebuild only.

- [ ] **Step 1: Rebuild backend container**

```bash
cd /Users/aghaammarkhan/EDGE/timetracker
docker-compose build backend && docker-compose up -d backend
```
Expected: no build errors.

- [ ] **Step 2: Check backend starts cleanly**

```bash
docker-compose logs --tail=30 backend
```
Expected: no `ImportError`, no `ValidationError`.

- [ ] **Step 3: TypeScript check dashboard**

```bash
cd /Users/aghaammarkhan/EDGE/timetracker/dashboard
npx tsc --noEmit
```
Expected: 0 errors.

- [ ] **Step 4: Rebuild dashboard container**

```bash
cd /Users/aghaammarkhan/EDGE/timetracker
docker-compose build dashboard && docker-compose up -d dashboard
```

- [ ] **Step 5: Smoke test logs endpoint returns timestamp_unix**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/v1/auth/admin/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@onedge.co","password":"YOUR_PASSWORD"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s "http://localhost:8000/v1/logs" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
rows = json.load(sys.stdin)
print(f'{len(rows)} events')
if rows:
    assert isinstance(rows[0]['timestamp_unix'], int), 'timestamp_unix must be int'
    print('timestamp_unix OK:', rows[0]['timestamp_unix'])
"
```
Expected: `N events` then `timestamp_unix OK: <integer>`.

- [ ] **Step 6: Smoke test devices endpoint returns last_seen_unix**

```bash
curl -s "http://localhost:8000/v1/devices" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
devices = json.load(sys.stdin)
if devices and devices[0].get('last_seen_unix'):
    print('last_seen_unix OK:', devices[0]['last_seen_unix'])
"
```

- [ ] **Step 7: Verify in browser**

Open `http://localhost:3000/logs`:
- Times display in PKT (local timezone), not UTC
- No date pre-selected; events show for last 48 h
- Select today's date → events for today appear

Open `http://localhost:3000/devices/<any-id>`:
- "Last seen X minutes ago" correct
- Select a date → timeline loads events for that day
- Screenshots show correct local time
- 7-day chart shows correct day labels (no off-by-one from DST)
