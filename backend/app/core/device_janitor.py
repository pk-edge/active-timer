"""
Device Janitor — background worker that auto-archives devices that have been
inactive longer than the company's configured threshold.

Architecture
────────────
  Worker wakes every hour and scans companies where inactive_device_days IS NOT NULL.
  For each such company, any device whose last_seen is older than the threshold
  (or that has never been seen) is set is_archived=True, is_active=False.

  The worker uses direct SQL so the scan is a single UPDATE per company rather
  than loading ORM objects for thousands of devices.

  Auth cache is NOT invalidated here — archived devices can no longer ingest
  events because is_active=False means the next heartbeat will get a 403 and
  the agent will stop. The cache entry expires naturally within its TTL.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

_INTERVAL = 3600        # run hourly
_STARTUP_DELAY = 90     # wait for migrations + seeds to finish on first boot

_worker_task: asyncio.Task | None = None


async def _do_archive() -> None:
    from app.db.base import BackgroundSessionLocal
    from sqlalchemy import text

    try:
        async with BackgroundSessionLocal() as db:
            companies = (await db.execute(text("""
                SELECT id, inactive_device_days
                FROM   companies
                WHERE  inactive_device_days IS NOT NULL
                  AND  inactive_device_days > 0
            """))).fetchall()

            if not companies:
                return

            total = 0
            for row in companies:
                cutoff = datetime.now(timezone.utc) - timedelta(days=int(row.inactive_device_days))
                archived = (await db.execute(text("""
                    UPDATE devices
                       SET is_archived = TRUE,
                           is_active   = FALSE
                     WHERE company_id = :cid
                       AND is_deleted  = FALSE
                       AND is_archived = FALSE
                       AND (last_seen IS NULL OR last_seen < :cutoff)
                    RETURNING id
                """), {"cid": str(row.id), "cutoff": cutoff})).fetchall()

                if archived:
                    log.info(
                        "device_janitor: archived %d device(s) for company %s "
                        "(inactive > %d days)",
                        len(archived), row.id, row.inactive_device_days,
                    )
                    total += len(archived)

            await db.commit()
            if total:
                log.info("device_janitor: cycle done — %d device(s) archived", total)

    except Exception:
        log.exception("device_janitor: archive cycle failed")


async def _loop() -> None:
    await asyncio.sleep(_STARTUP_DELAY)
    while True:
        await _do_archive()
        await asyncio.sleep(_INTERVAL)


async def start() -> None:
    """Idempotent: a second call while the loop is alive is a no-op (no orphaned
    second loop)."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop(), name="device_janitor")
    log.info("device_janitor: worker started (interval=%ds)", _INTERVAL)


async def stop() -> None:
    """Idempotent: tolerates never-started / already-stopped."""
    global _worker_task
    task, _worker_task = _worker_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("device_janitor: worker had failed")
    log.info("device_janitor: worker stopped")
