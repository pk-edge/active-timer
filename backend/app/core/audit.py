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
        # Capture the role name at the time of the action — role assignments
        # can change later, so we store it immutably here.
        actor_role: str | None = None
        role_obj = getattr(actor, "role_obj", None)
        if role_obj:
            actor_role = getattr(role_obj, "name", None)
        if not actor_role:
            actor_role = getattr(actor, "role", None)

        db.add(AuditLog(
            company_id=getattr(actor, "company_id", None),
            actor_employee_id=getattr(actor, "id", None),
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            audit_metadata=metadata,
        ))
        await db.flush()
    except Exception as e:  # pragma: no cover - logging only
        log.warning("Audit write failed for %s: %s", action, e)
