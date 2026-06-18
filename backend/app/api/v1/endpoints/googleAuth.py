"""
Google OAuth 2.0 — server-side authorization code flow.

Flow
────
1. GET  /v1/auth/google           → redirect browser to Google consent screen
2. GET  /v1/auth/google/callback  → exchange code, validate domain, create/link
                                    employee, issue JWT, redirect to dashboard
3. POST /v1/auth/set-password     → lets Google-only users add an email password

Configuration (.env)
────────────────────
GOOGLE_CLIENT_ID      — from Google Cloud Console
GOOGLE_CLIENT_SECRET  — from Google Cloud Console (or use encrypted secrets file)
GOOGLE_REDIRECT_URI   — OPTIONAL. Blank → derived from the request
                        (request.url_for). Whatever value is used MUST be
                        registered in Google Cloud Console.
DASHBOARD_URL         — OPTIONAL fallback. The user is normally returned to the
                        allowlisted origin they signed in from, so one backend
                        serves localhost / staging / prod without per-env hosts.
ALLOWED_EMAIL_DOMAIN  — only this domain is accepted (default: onedge.co)

Multi-environment redirects
───────────────────────────
Both the Google callback URL and the post-login dashboard URL are derived from
the live request (the dashboard origin is validated against CORS_ORIGINS so the
token can never be redirected off-allowlist). This requires uvicorn to run with
--proxy-headers so the public https host behind the load balancer is seen.

Security
────────
• Credentials: loaded from secrets/google_credentials.enc when SECRETS_KEY is set,
  otherwise falls back to GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars.
• CSRF: HMAC-signed state token (10-minute expiry, no DB storage).
• Domain: rejects any email not ending in @ALLOWED_EMAIL_DOMAIN.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.secrets import load_google_credentials
from app.core.security import create_admin_token, hash_password_async
from app.db.models import Company, Employee, Role

log = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USER_URL  = "https://www.googleapis.com/oauth2/v3/userinfo"


# ── Environment URLs ──────────────────────────────────────────────────────────
# Hard-coded per-environment endpoints. The environment is chosen by matching
# the dashboard origin the user signed in from, so one backend image serves
# localhost, staging and prod with the EXACT URLs registered in Google Cloud
# Console — no dependence on the proxy forwarding the Host header. The Google
# callback URL for the matched env is used as-is, and the user is returned to
# that env's dashboard. (Add a row here for any new environment.)
_ENVIRONMENTS = [
    {"dashboard": "https://tracker.onedge.co",
     "api":       "https://api.tracker.onedge.co"},
    {"dashboard": "https://tracker.staging.onedge.co",
     "api":       "https://api.tracker.staging.onedge.co"},
    {"dashboard": "http://localhost:3002",
     "api":       "http://localhost:8000"},
]


def _allowed_origins() -> set[str]:
    # Hard-coded dashboards are always trusted; CORS_ORIGINS covers LAN/dev too.
    return {e["dashboard"].rstrip("/") for e in _ENVIRONMENTS} | {
        o.rstrip("/") for o in settings.CORS_ORIGINS
    }


def _safe_origin(candidate: str) -> str:
    """Return `candidate`'s scheme://host IF it is an allowlisted origin, else ""
    (caller falls back to settings.DASHBOARD_URL). Prevents the token redirect
    from being pointed at an arbitrary attacker origin."""
    if not candidate:
        return ""
    p = urlsplit(candidate)
    if not p.scheme or not p.netloc:
        return ""
    origin = f"{p.scheme}://{p.netloc}"
    return origin if origin in _allowed_origins() else ""


def _match_env(dashboard_origin: str) -> dict | None:
    o = dashboard_origin.rstrip("/")
    return next((e for e in _ENVIRONMENTS if e["dashboard"].rstrip("/") == o), None)


def _dashboard_for_request(request: Request) -> str:
    """Best-effort dashboard for THIS backend — used as the error/fallback target
    when there is no validated origin from state (so an error never bounces to
    localhost on staging/prod). Maps the backend's own host to its environment's
    dashboard, then falls back to settings.DASHBOARD_URL."""
    host = request.url.netloc
    for e in _ENVIRONMENTS:
        if urlsplit(e["api"]).netloc == host:
            return e["dashboard"]
    return settings.DASHBOARD_URL or _ENVIRONMENTS[-1]["dashboard"]


def _redirect_uri(request: Request, dashboard_origin: str = "") -> str:
    """The Google OAuth callback URL. Priority:
       1. explicit GOOGLE_REDIRECT_URI override (if set);
       2. the hard-coded api host for the matched environment;
       3. derived from the request (last-resort fallback).
    MUST be byte-identical at /google start and at the token exchange."""
    if settings.GOOGLE_REDIRECT_URI:
        return settings.GOOGLE_REDIRECT_URI
    env = _match_env(dashboard_origin)
    if env:
        return f"{env['api']}/v1/auth/google/callback"
    return str(request.url_for("google_oauth_callback"))


# ── CSRF state ────────────────────────────────────────────────────────────────
# State carries the (already allowlist-validated) dashboard origin so the
# callback knows where to send the user back, tamper-proofed by the HMAC. The
# field delimiter is "." — safe because every field is digits / base64url / hex
# and none can contain a dot.

def _make_state(dashboard_origin: str = "") -> str:
    ts      = str(int(time.time()))
    nonce   = secrets.token_urlsafe(16)
    org_b64 = base64.urlsafe_b64encode(dashboard_origin.encode()).decode().rstrip("=")
    msg     = f"{ts}.{nonce}.{org_b64}".encode()
    sig     = hmac.new(settings.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{nonce}.{org_b64}.{sig}"


def _verify_state(state: str, max_age: int = 600) -> str:
    """Validate the signed state and return the dashboard origin embedded at
    start ("" if none). Raises HTTPException on tamper / expiry / malformed."""
    try:
        ts_str, nonce, org_b64, sig = state.split(".")
        if time.time() - int(ts_str) > max_age:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth state expired")
        msg      = f"{ts_str}.{nonce}.{org_b64}".encode()
        expected = hmac.new(settings.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")
        if not org_b64:
            return ""
        pad = "=" * (-len(org_b64) % 4)
        return base64.urlsafe_b64decode(org_b64 + pad).decode()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed OAuth state")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/google")
async def google_oauth_start(request: Request, origin: str = Query(default="")):
    creds = load_google_credentials()
    if not creds["client_id"]:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google OAuth not configured")

    # Remember where to send the user back: the dashboard passes its own origin
    # (?origin=), with the Referer as a fallback. Either way it is allowlisted.
    dash_origin = _safe_origin(origin or request.headers.get("referer", ""))

    params = urlencode({
        "client_id":     creds["client_id"],
        "redirect_uri":  _redirect_uri(request, dash_origin),
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "online",
        "prompt":        "select_account",
        "state":         _make_state(dash_origin),
    })
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{params}")


@router.get("/google/callback")
async def google_oauth_callback(
    request: Request,
    code:  str = Query(...),
    state: str = Query(...),
    db:    AsyncSession = Depends(get_db),
):
    # Fallback dashboard for error paths = THIS backend's own environment, so a
    # failure never bounces the user to localhost on staging/prod.
    dash = _dashboard_for_request(request)

    try:
        # Prefer the origin the user actually signed in from (carried tamper-proof
        # in the signed state); re-check the allowlist in case it changed.
        origin = _verify_state(state)
        if origin and origin in _allowed_origins():
            dash = origin
    except HTTPException:
        return RedirectResponse(f"{dash}/login?error=invalid_state")

    creds = load_google_credentials()
    if not creds["client_id"] or not creds["client_secret"]:
        return RedirectResponse(f"{dash}/login?error=oauth_not_configured")

    # ── Exchange code for tokens ──────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
                "code":          code,
                "client_id":     creds["client_id"],
                "client_secret": creds["client_secret"],
                # MUST be byte-identical to the redirect_uri sent at /google start
                # — same origin from state → same matched environment → same URL.
                "redirect_uri":  _redirect_uri(request, origin),
                "grant_type":    "authorization_code",
            })
        if token_resp.status_code != 200:
            log.warning("Google token exchange failed: %s", token_resp.text)
            return RedirectResponse(f"{dash}/login?error=google_token_failed")
        tokens = token_resp.json()
    except Exception as e:
        log.error("Google token exchange error: %s", e)
        return RedirectResponse(f"{dash}/login?error=google_unreachable")

    # ── Fetch userinfo ────────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            ui_resp = await client.get(
                _GOOGLE_USER_URL,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
        if ui_resp.status_code != 200:
            return RedirectResponse(f"{dash}/login?error=google_userinfo_failed")
        info = ui_resp.json()
    except Exception as e:
        log.error("Google userinfo error: %s", e)
        return RedirectResponse(f"{dash}/login?error=google_unreachable")

    email      = (info.get("email") or "").lower().strip()
    name       = info.get("name") or email.split("@")[0]
    google_sub = info.get("sub", "")

    if not email or not google_sub:
        return RedirectResponse(f"{dash}/login?error=google_missing_email")

    # Only accept emails Google itself has verified — never trust an unverified
    # address (it could be a value the account holder set but does not own).
    if not info.get("email_verified", False):
        log.warning("Rejected unverified Google email: %s", email)
        return RedirectResponse(f"{dash}/login?error=email_unverified")

    # ── Domain enforcement ────────────────────────────────────────────────────
    if not email.endswith(f"@{settings.ALLOWED_EMAIL_DOMAIN}"):
        log.warning("Rejected Google sign-in: %s", email)
        return RedirectResponse(f"{dash}/login?error=domain_not_allowed")

    # ── Find or create employee ───────────────────────────────────────────────
    # Deterministic company pick (oldest = the founding company) so a new user
    # is never attached to an arbitrary tenant under multi-company deployments.
    company_row = await db.execute(
        select(Company).order_by(Company.created_at.asc()).limit(1)
    )
    company     = company_row.scalar_one_or_none()
    if not company:
        return RedirectResponse(f"{dash}/login?error=company_not_found")

    emp_row  = await db.execute(select(Employee).where(Employee.email == email))
    employee = emp_row.scalar_one_or_none()
    new_user = False

    if employee:
        # Reject disabled accounts BEFORE persisting anything — never mutate a
        # deactivated/deleted record's identity (it could later be reactivated).
        if not employee.is_active or employee.is_deleted:
            return RedirectResponse(f"{dash}/login?error=account_inactive")

        # Block talent (device-only) accounts — auto-provisioned by enrollment with the
        # "Talent" system role. They must never reach the dashboard portal.
        is_talent = (
            employee.role_obj is not None
            and getattr(employee.role_obj, "is_system", False)
            and getattr(employee.role_obj, "name", "") == "Talent"
        )
        has_no_portal_role = (
            employee.role_id is None
            and employee.role not in ("admin", "manager", "viewer")
        )
        if is_talent or has_no_portal_role:
            log.warning("Blocked portal access for talent/device-only account: %s", email)
            return RedirectResponse(f"{dash}/login?error=portal_access_restricted")

        if employee.google_sub != google_sub:
            employee.google_sub = google_sub
            await db.commit()
    else:
        new_user = True
        # New SSO users default to VIEWER (read-only, group-scoped). They are
        # NEVER auto-provisioned as admin and get no edit access — an existing
        # admin must explicitly elevate the role before they can change anything.
        viewer_role = (await db.execute(
            select(Role).where(
                Role.company_id == company.id,
                Role.name == "Viewer",
                Role.is_deleted == False,  # noqa: E712
            )
        )).scalar_one_or_none()
        employee = Employee(
            company_id=company.id,
            email=email,
            name=name,
            google_sub=google_sub,
            role="viewer",
            role_id=viewer_role.id if viewer_role else None,
            is_active=True,
            is_approved=True,
        )
        db.add(employee)
        await db.commit()
        await db.refresh(employee)
        log.info("Auto-provisioned Google user as Viewer (read-only): %s", email)

    jwt_token = create_admin_token(str(employee.id), employee.role, str(employee.company_id))
    return RedirectResponse(
        f"{dash}/login?token={jwt_token}&new_user={str(new_user).lower()}"
    )


# ── Set password ──────────────────────────────────────────────────────────────

class SetPasswordRequest(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def _min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@router.post("/set-password", status_code=status.HTTP_204_NO_CONTENT)
async def set_password(
    body: SetPasswordRequest,
    user: Employee = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    user.password_hash = await hash_password_async(body.password)
    await db.commit()
