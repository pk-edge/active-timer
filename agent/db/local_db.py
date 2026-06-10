"""
LocalDB — SQLite-backed offline-first queue.

Design:
  - Every captured event is inserted immediately as synced=0.
  - The sync worker reads batches of unsynced rows and marks them synced=1
    only after the server returns HTTP 200.
  - Screenshots are stored separately; their S3 key is recorded here.
  - Uses WAL mode for safe concurrent writes from multiple threads.
"""

import logging
import sqlite3
import threading
import uuid
import time
from pathlib import Path

log = logging.getLogger("local_db")

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

-- Persistent per-day accumulator so restarts don't lose accumulated time
CREATE TABLE IF NOT EXISTS daily_totals (
    date         TEXT    PRIMARY KEY,   -- YYYY-MM-DD in local time
    device_id    TEXT    NOT NULL,
    active_secs  INTEGER DEFAULT 0,
    idle_secs    INTEGER DEFAULT 0,
    updated_at   INTEGER NOT NULL
);

-- Agent-cached blocked-URL policy fetched from the backend
CREATE TABLE IF NOT EXISTS blocked_urls (
    id           TEXT    PRIMARY KEY,
    pattern      TEXT    NOT NULL,
    target_type  TEXT    NOT NULL DEFAULT 'all',  -- 'all' | 'device'
    created_at   INTEGER NOT NULL
);
"""


class LocalDB:
    def __init__(self, db_path: Path):
        self.db_path    = str(db_path)
        self._lock      = threading.Lock()
        self._device_id = ""

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
        bad_rows = conn.execute(
            "SELECT COUNT(*) FROM events WHERE CAST(strftime('%s', ts) AS INTEGER) IS NULL"
        ).fetchone()[0]
        if bad_rows:
            log.warning("Migration: %d event rows have unparseable timestamps — will be set to 0", bad_rows)
        bad_ss = conn.execute(
            "SELECT COUNT(*) FROM screenshots WHERE CAST(strftime('%s', captured_at) AS INTEGER) IS NULL"
        ).fetchone()[0]
        if bad_ss:
            log.warning("Migration: %d screenshot rows have unparseable timestamps — will be set to 0", bad_ss)
        conn.executescript("""
            BEGIN IMMEDIATE;
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
            COMMIT;
        """)
        log.info("LocalDB migrated to v2 (unix integer timestamps)")

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        log.info("LocalDB initialized at %s", self.db_path)

    def set_device_id(self, device_id: str) -> None:
        self._device_id = device_id

    @property
    def device_id(self) -> str:
        return self._device_id

    # ── Events ────────────────────────────────────────────────────────────

    def insert_event(self, event: dict) -> str:
        if not self._device_id:
            log.debug("insert_event skipped — not yet enrolled")
            return ""

        row_id = str(uuid.uuid4())
        # Honor the capture-time timestamp when provided (e.g. real browser
        # visit time from history) — fall back to insert time.
        ts = event.get("ts") or int(time.time())
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
                    int(ts),
                ),
            )
        return row_id

    def get_unsynced_events(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE synced=0 ORDER BY ts ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_events_synced(self, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        with self._lock, self._conn() as conn:
            conn.execute(
                f"UPDATE events SET synced=1 WHERE id IN ({placeholders})", ids
            )

    def purge_synced_events(self, keep_recent: int = 5000) -> None:
        """Keep the most recent N synced events; delete older."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """DELETE FROM events
                   WHERE synced=1
                   AND id NOT IN (
                       SELECT id FROM events
                       WHERE synced=1
                       ORDER BY ts DESC
                       LIMIT ?
                   )""",
                (keep_recent,),
            )

    def unsynced_count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE synced=0"
            ).fetchone()[0]

    # ── Screenshots ───────────────────────────────────────────────────────

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

    def get_pending_screenshots(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM screenshots WHERE uploaded=0 ORDER BY captured_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_screenshot_uploaded(self, screenshot_id: str, s3_key: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE screenshots SET uploaded=1, s3_key=? WHERE id=?",
                (s3_key, screenshot_id),
            )

    def purge_old_screenshots(self, days: int = 30) -> None:
        """Delete local screenshot files older than N days after upload."""
        import os
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, local_path FROM screenshots
                   WHERE uploaded=1
                   AND created_at < datetime('now', ?)""",
                (f"-{days} days",),
            ).fetchall()
        for row in rows:
            try:
                os.remove(row["local_path"])
            except FileNotFoundError:
                pass
            with self._lock, self._conn() as conn:
                conn.execute("DELETE FROM screenshots WHERE id=?", (row["id"],))

    # ── Attendance sessions ───────────────────────────────────────────────

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

    def end_session(self, session_id: str, active_secs: int) -> None:
        if not session_id:
            return
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE attendance_sessions SET ended_at=?, active_secs=? WHERE id=?",
                (int(time.time()), active_secs, session_id),
            )
        log.info("Attendance session ended: %s (%ds active)", session_id, active_secs)

    def get_unsynced_sessions(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM attendance_sessions WHERE synced=0 AND ended_at IS NOT NULL "
                "ORDER BY started_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_sessions_synced(self, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        with self._lock, self._conn() as conn:
            conn.execute(
                f"UPDATE attendance_sessions SET synced=1 WHERE id IN ({placeholders})", ids
            )

    def get_today_active_secs_any_device(self) -> int:
        """Return today's best accumulated active seconds across ALL device IDs.

        After a re-enrollment the agent gets a new device_id, so
        daily_totals rows from the old id would be invisible to
        get_today_active_secs().  This method returns the maximum across
        all today rows so accumulated time is never lost on re-enrollment.
        """
        from datetime import date
        today = str(date.today())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(active_secs), 0) FROM daily_totals WHERE date = ?",
                (today,),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_today_active_secs(self) -> int:
        """Return today's accumulated active seconds.
        Prefers the daily_totals accumulator (updated every minute) over the
        session sum so restarts always resume from the correct baseline.
        """
        # 1. Check fast accumulator first
        accumulated = self.get_today_accumulated_secs()
        if accumulated > 0:
            return accumulated
        # 2. Fall back to summing ended sessions (legacy / first run)
        from datetime import datetime
        today_start = int(datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(active_secs), 0) FROM attendance_sessions "
                "WHERE device_id = ? AND started_at >= ? AND ended_at IS NOT NULL",
                (self._device_id, today_start),
            ).fetchone()
        return int(row[0])

    # ── Daily totals (crash-safe accumulator) ────────────────────────────

    def flush_daily_secs(self, active_delta: int, idle_delta: int = 0) -> None:
        """Add active/idle seconds to today's persistent accumulator.
        Called periodically by the widget so time is not lost on crash.
        """
        if not self._device_id or active_delta < 0:
            return
        from datetime import date
        today = str(date.today())
        with self._lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO daily_totals (date, device_id, active_secs, idle_secs, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    active_secs = active_secs + excluded.active_secs,
                    idle_secs   = idle_secs   + excluded.idle_secs,
                    updated_at  = excluded.updated_at
            """, (today, self._device_id, active_delta, idle_delta, int(time.time())))

    def flush_daily_secs_absolute(self, active_secs: int) -> None:
        """Overwrite (not add) today's accumulated value with an authoritative figure.
        Called on startup after syncing with the backend so the local accumulator
        matches the portal exactly.
        """
        if not self._device_id:
            return
        from datetime import date
        today = str(date.today())
        with self._lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO daily_totals (date, device_id, active_secs, idle_secs, updated_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(date) DO UPDATE SET
                    active_secs = excluded.active_secs,
                    updated_at  = excluded.updated_at
            """, (today, self._device_id, active_secs, int(time.time())))

    def get_today_accumulated_secs(self) -> int:
        """Return persisted active seconds for today — used on restart to resume timer."""
        if not self._device_id:
            return 0
        from datetime import date
        today = str(date.today())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT active_secs FROM daily_totals WHERE date = ? AND device_id = ?",
                (today, self._device_id),
            ).fetchone()
        return int(row[0]) if row else 0

    # ── Blocked URLs cache ────────────────────────────────────────────────

    def update_blocked_urls(self, entries: list[dict]) -> None:
        """Replace the local blocked-URL cache with the latest from the server."""
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM blocked_urls")
            conn.executemany(
                "INSERT OR IGNORE INTO blocked_urls (id, pattern, target_type, created_at) VALUES (?,?,?,?)",
                [(e["id"], e["pattern"], e.get("target_type", "all"), int(time.time())) for e in entries],
            )

    def get_blocked_url_patterns(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT pattern FROM blocked_urls").fetchall()
        return [r[0] for r in rows]

    # ── Internal ──────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
