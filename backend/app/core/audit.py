"""Best-effort audit logging. Never raises into the request path."""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog

log = logging.getLogger(__name__)


async def record(
    db: AsyncSession,
    actor,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Write an audit row. Swallows errors so auditing never breaks a mutation."""
    try:
        db.add(AuditLog(
            company_id=getattr(actor, "company_id", None),
            actor_employee_id=getattr(actor, "id", None),
            action=action,
            target_type=target_type,
            target_id=target_id,
            audit_metadata=metadata,
        ))
        await db.flush()
    except Exception as e:  # pragma: no cover - logging only
        log.warning("Audit write failed for %s: %s", action, e)
