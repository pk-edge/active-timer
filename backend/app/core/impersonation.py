"""Super-Admin impersonation: the structural gate and the revocation registry.

Two things live here because they are the security core of the feature and
should be readable in one sitting:

1. `is_super_admin()` — the ONLY test for who may impersonate. It is deliberately
   structural (the seeded system role with company-wide scope) rather than a
   grantable (module, action) permission. Impersonation must not be delegable
   through the Roles UI, so there is no permission pair to grant and nothing about
   the feature is visible to anyone who isn't a Super Admin. This is a conscious
   carve-out from the "every feature ships with its permission" rule in CLAUDE.md.

2. A Redis-backed session registry keyed by the token's `jti`. The rest of this
   codebase issues stateless JWTs with no revocation path — an admin token is
   valid until it expires, full stop. That is unacceptable for impersonation,
   where "Exit" has to actually end the session, so impersonation tokens (and
   ONLY impersonation tokens) are checked against this registry on every request.

   Consequences of scoping the check to `imp` tokens:
     • the normal admin auth path is untouched — no extra Redis hop, no new
       failure mode for ordinary logins;
     • if Redis is down, impersonation fails CLOSED (nobody can start or continue
       an impersonated session) while normal authentication keeps working.
"""
import logging

from app.core.RedisClient import get_client
from app.db.models import Employee

log = logging.getLogger(__name__)

_KEY_PREFIX = "tt:imp:"


def session_key(jti: str) -> str:
    return f"{_KEY_PREFIX}{jti}"


# ── Who may impersonate ───────────────────────────────────────────────────────

def is_super_admin(user: Employee) -> bool:
    """True only for the seeded, company-wide system role.

    Mirrors the test `app/api/deps.py` already uses to grant the full permission
    matrix. A custom role can be given `scope_type='all'`, but it cannot be given
    `is_system=True` through any API, so this cannot be escalated into.
    """
    role = getattr(user, "role_obj", None)
    if role is None:
        # Legacy pre-RBAC accounts (role='admin'/'manager' with no role row) are
        # linked to the Super Admin role by seed_rbac on boot. Until that runs we
        # deliberately refuse rather than guess.
        return False
    return bool(role.scope_type == "all" and role.is_system)


def is_impersonating(user: Employee) -> bool:
    return getattr(user, "_impersonator_id", None) is not None


# ── Session registry (revocation) ─────────────────────────────────────────────

async def register_session(jti: str, admin_id: str, ttl_secs: int) -> bool:
    """Record an active impersonation session. False if Redis is unavailable.

    The caller MUST refuse to issue a token when this returns False: a token we
    cannot revoke is a token that outlives its Exit button.
    """
    r = get_client()
    if r is None:
        return False
    try:
        await r.setex(session_key(jti), ttl_secs, admin_id)
        return True
    except Exception as exc:  # noqa: BLE001 - Redis hiccup must not 500
        log.warning("Impersonation session register failed: %s", exc)
        return False


async def is_session_active(jti: str) -> bool:
    """Whether this impersonation session is still live. Fails CLOSED."""
    r = get_client()
    if r is None:
        return False
    try:
        return await r.get(session_key(jti)) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("Impersonation session lookup failed: %s", exc)
        return False


async def revoke_session(jti: str) -> None:
    """End a session. Safe to call repeatedly and when Redis is already gone —
    an unreachable Redis means `is_session_active` is already refusing the token."""
    r = get_client()
    if r is None:
        return
    try:
        await r.delete(session_key(jti))
    except Exception as exc:  # noqa: BLE001
        log.warning("Impersonation session revoke failed: %s", exc)
