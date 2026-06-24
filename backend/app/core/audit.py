"""Best-effort audit logging — written on a BACKGROUND worker, never on the
request path.

Audit rows are "application logs": useful, but not worth making a user wait for.
Previously every mutation did an extra INSERT + flush inside the request's own
transaction (and held its pooled connection a little longer). Now `record()`
captures the row as plain primitives and drops it on an in-process queue that a
single background task drains and writes in batches on the dedicated ingest
writer pool — so the HTTP request returns without touching audit storage at all.

Design notes
────────────
• record() keeps its original signature (db, actor, …) so the ~34 call sites are
  unchanged. The `db` arg is only used by the inline fallback below.
• Fallback: if the background worker is NOT running (e.g. unit tests that never
  start it), record() writes inline with the caller's session — preserving the
  old, fully-synchronous behaviour so tests still observe the row immediately.
• Never raises into the request path. Audit loss is logged, never fatal.
• Best-effort durability: a row is enqueued AFTER the caller has done its work
  but the caller commits its own mutation separately. This matches "background
  task" semantics — auditing must never roll back or delay the real operation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog

log = logging.getLogger(__name__)

_QUEUE_MAX = 10_000
_BATCH = 200
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None


def _actor_role(actor) -> str | None:
    # Capture the role name AT THE TIME of the action — role assignments can
    # change later, so we store it immutably.
    role_obj = getattr(actor, "role_obj", None)
    if role_obj is not None:
        name = getattr(role_obj, "name", None)
        if name:
            return name
    return getattr(actor, "role", None)


def _row_from(actor, action, target_type, target_id, metadata) -> dict[str, Any]:
    """Snapshot everything as primitives now — the actor's ORM session may be
    closed by the time the background worker writes the row."""
    return {
        "company_id": getattr(actor, "company_id", None),
        "actor_employee_id": getattr(actor, "id", None),
        "actor_role": _actor_role(actor),
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "audit_metadata": metadata,
    }


# ── Lifecycle (wired into app lifespan) ───────────────────────────────────────

async def start() -> None:
    global _queue, _worker
    if _worker is not None:
        return
    _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _worker = asyncio.create_task(_run(), name="audit-writer")
    log.info("Audit writer started")


async def stop() -> None:
    global _worker
    if _worker is None:
        return
    await _drain_remaining()
    _worker.cancel()
    try:
        await _worker
    except asyncio.CancelledError:
        pass
    _worker = None
    log.info("Audit writer stopped")


# ── Public API (unchanged signature) ──────────────────────────────────────────

async def record(
    db: AsyncSession,
    actor,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Enqueue an audit row for background write. Falls back to an inline write
    (using the caller's session) when the background worker isn't running."""
    try:
        row = _row_from(actor, action, target_type, target_id, metadata)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("Audit snapshot failed for %s: %s", action, e)
        return

    if _queue is None:
        # Worker not started (tests / scripts) — preserve the old inline behaviour.
        await _write_inline(db, row)
        return

    try:
        _queue.put_nowait(row)
    except asyncio.QueueFull:
        # Extreme backlog: don't lose the row and don't block — write it inline.
        log.warning("Audit queue full — writing %s inline", action)
        await _write_inline(db, row)


# ── Background worker ─────────────────────────────────────────────────────────

async def _run() -> None:
    from app.db.base import IngestSessionLocal
    assert _queue is not None
    try:
        while True:
            first = await _queue.get()
            batch = [first]
            # Greedily drain whatever else is already waiting (no extra latency).
            while len(batch) < _BATCH:
                try:
                    batch.append(_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                async with IngestSessionLocal() as db:
                    db.add_all([AuditLog(**r) for r in batch])
                    await db.commit()
            except Exception as e:  # pragma: no cover - best effort
                log.warning("Audit batch write failed (%d rows dropped): %s", len(batch), e)
    except asyncio.CancelledError:
        pass


async def _drain_remaining() -> None:
    if _queue is None or _queue.empty():
        return
    from app.db.base import IngestSessionLocal
    rows = []
    while not _queue.empty():
        try:
            rows.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if not rows:
        return
    try:
        async with IngestSessionLocal() as db:
            db.add_all([AuditLog(**r) for r in rows])
            await db.commit()
    except Exception as e:  # pragma: no cover
        log.warning("Audit drain-on-shutdown failed (%d rows): %s", len(rows), e)


async def _write_inline(db: AsyncSession, row: dict) -> None:
    try:
        db.add(AuditLog(**row))
        await db.flush()
    except Exception as e:  # pragma: no cover - logging only
        log.warning("Audit inline write failed for %s: %s", row.get("action"), e)
