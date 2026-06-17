"""
SyncWorker — uploads queued events and screenshots to the backend.

Architecture:
  - Event-driven: capture threads set shared["sync_trigger"] when they
    insert events. SyncWorker wakes immediately instead of waiting out a
    fixed timer.
  - Bounded wait: if no trigger fires, syncs anyway every SYNC_INTERVAL_SECS
    (5 s) to guarantee last_seen stays fresh.
  - Debounce: after waking, waits SYNC_DEBOUNCE_SECS (0.5 s) to let any
    burst of events (e.g. rapid app switches) settle into one batch.
  - Persistent HTTP session: httpx.Client is created once and reused,
    so the TCP/TLS connection to the backend is kept alive across syncs.
  - Screenshots on a separate longer interval — never tied to event sync.
  - Exponential backoff on failure — triggers are ignored during cooldown
    so a broken backend is not hammered.
"""

import logging
import time

import httpx

from config import Config
import identity
import enrollment

log = logging.getLogger("sync_worker")


class SyncWorker:
    def __init__(self, shared: dict):
        self._db      = shared["db"]
        self._shared  = shared
        self._pause   = shared["pause_event"]
        self._stop    = shared["stop_event"]
        self._trigger = shared["sync_trigger"]
        self._fails   = 0
        self._last_screenshot_sync = 0.0
        self._last_policy_sync     = 0.0
        self._last_purge           = 0.0

        # Persistent HTTP client — TCP connection reused across every sync call.
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=5, read=15, write=15, pool=5),
        )

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("SyncWorker started (interval=%ds debounce=%.1fs)",
                 Config.SYNC_INTERVAL_SECS, Config.SYNC_DEBOUNCE_SECS)
        try:
            while not self._stop.is_set():
                self._wait()
                if self._stop.is_set():
                    break
                self._sync_events()
                self._maybe_sync_screenshots()
                self._maybe_sync_policy()
                self._maybe_purge()
        finally:
            self._http.close()
            log.info("SyncWorker stopped")

    def _wait(self) -> None:
        """
        Block until a capture thread signals new data OR the interval elapses.
        During backoff, ignores triggers and sleeps the full backoff delay.
        """
        if self._fails > 0:
            delay = min(
                Config.SYNC_INTERVAL_SECS * (Config.SYNC_BACKOFF_BASE ** self._fails),
                Config.SYNC_BACKOFF_CAP,
            )
            # Wake early only for clean shutdown — do not hammer a broken backend.
            self._stop.wait(timeout=delay)
            return

        # Happy path: wake on trigger (new event inserted) or 5 s timeout.
        triggered = self._trigger.wait(timeout=Config.SYNC_INTERVAL_SECS)
        if triggered:
            self._trigger.clear()
            # Debounce: let a rapid burst of events land before we read the batch.
            if not self._stop.wait(timeout=Config.SYNC_DEBOUNCE_SECS):
                pass  # stop not set — debounce elapsed normally

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _auth_headers(self) -> dict | None:
        token = identity.load().get("jwt_token", "")
        return {"Authorization": f"Bearer {token}"} if token else None

    def _handle_unauthorized(self) -> None:
        log.warning("Got 401 from server — attempting re-enrollment")
        identity.clear()
        try:
            new = enrollment.ensure_enrolled(max_attempts=3)
            self._db.set_device_id(new["device_id"])
        except Exception as e:
            log.error("Re-enrollment failed: %s", e)

    # ── Event sync ───────────────────────────────────────────────────────────

    def _sync_events(self) -> None:
        headers = self._auth_headers()
        if headers is None:
            # Not enrolled yet — keep UI on connecting, not syncing/offline.
            if self._shared.get("sync_status") not in ("error", "enrolling"):
                self._shared["sync_status"] = "enrolling"
            return

        batch = self._db.get_unsynced_events(Config.SYNC_BATCH_SIZE)
        if not batch:
            self._shared["sync_status"] = "ok"
            return

        payload = {
            "events": [
                {
                    "id":           e["id"],
                    "event_type":   e["event_type"],
                    "app_name":     e["app_name"],
                    "window_title": e["window_title"],
                    "url":          e["url"],
                    "duration":     e["duration"],
                    "timestamp":    e["ts"],
                }
                for e in batch
            ],
        }

        try:
            r = self._http.post(Config.INGEST_ENDPOINT, json=payload, headers=headers)
            if r.status_code == 401:
                self._handle_unauthorized()
                self._fails += 1
                self._shared["sync_status"] = "offline" if self._fails > 5 else "syncing"
                return
            r.raise_for_status()
            self._db.mark_events_synced([e["id"] for e in batch])
            self._fails = 0
            self._shared["sync_status"] = "ok"
            log.debug("Synced %d events", len(batch))

            # Full batch ⇒ an offline backlog is draining: re-arm the trigger
            # so the next batch goes immediately instead of after the 5s wait.
            if len(batch) == Config.SYNC_BATCH_SIZE:
                self._trigger.set()

        except Exception as e:
            self._fails += 1
            self._shared["sync_status"] = "offline" if self._fails > 5 else "syncing"
            log.warning("Sync failed (attempt %d): %s", self._fails, e)

    # ── Screenshot sync ──────────────────────────────────────────────────────

    def _maybe_sync_screenshots(self) -> None:
        # Feature switch — also stops devices with an old local backlog from
        # uploading it (POST /v1/screenshots + S3 PUT per shot).
        if not Config.SCREENSHOTS_ENABLED:
            return
        now = time.time()
        if now - self._last_screenshot_sync < Config.SCREENSHOT_SYNC_SECS:
            return
        self._last_screenshot_sync = now
        for ss in self._db.get_pending_screenshots(limit=5):
            self._upload_screenshot(ss)

    # ── Local retention ──────────────────────────────────────────────────────

    def _maybe_purge(self) -> None:
        """Hourly retention pass: flush backend-confirmed records older than
        LOCAL_RETENTION_DAYS; unsynced records are kept until they sync."""
        now = time.time()
        if now - self._last_purge < Config.PURGE_INTERVAL_SECS:
            return
        self._last_purge = now
        try:
            removed = self._db.purge_synced_records(Config.LOCAL_RETENTION_DAYS)
            if any(removed.values()):
                log.info("Retention purge (>%dd, synced only): %s",
                         Config.LOCAL_RETENTION_DAYS, removed)
        except Exception as e:
            log.warning("Retention purge failed: %s", e)

    # ── Policy sync ──────────────────────────────────────────────────────────

    def _maybe_sync_policy(self) -> None:
        now = time.time()
        if now - self._last_policy_sync < Config.POLICY_SYNC_SECS:
            return
        self._last_policy_sync = now

        headers = self._auth_headers()
        if not headers:
            return
        try:
            r = self._http.get(Config.POLICY_ENDPOINT, headers=headers, timeout=10)
            if r.status_code == 200:
                entries = r.json()  # list[{id, pattern, target_type}]
                mgr = self._shared.get("policy_manager")
                if mgr:
                    mgr.update(entries)
                else:
                    self._db.update_blocked_urls(entries)
                log.debug("Policy synced: %d blocked URLs", len(entries))
            elif r.status_code == 404:
                pass  # endpoint not yet deployed — silent skip
        except Exception as e:
            log.debug("Policy sync failed: %s", e)

    def _upload_screenshot(self, ss: dict) -> None:
        headers = self._auth_headers()
        if headers is None:
            return
        try:
            r = self._http.post(
                Config.UPLOAD_ENDPOINT,
                json={
                    "screenshot_id": ss["id"],
                    "captured_at":   ss["captured_at"],
                    "file_size":     ss["file_size"],
                },
                headers=headers,
            )
            if r.status_code == 401:
                self._handle_unauthorized()
                return
            r.raise_for_status()
            data = r.json()

            with open(ss["local_path"], "rb") as f:
                put_r = self._http.put(
                    data["upload_url"],
                    content=f.read(),
                    headers={"Content-Type": "image/png"},
                )
            put_r.raise_for_status()

            self._db.mark_screenshot_uploaded(ss["id"], data["s3_key"])
            log.debug("Uploaded screenshot %s", ss["id"])

        except FileNotFoundError:
            self._db.mark_screenshot_uploaded(ss["id"], "missing")
        except Exception as e:
            log.warning("Screenshot upload failed: %s", e)
