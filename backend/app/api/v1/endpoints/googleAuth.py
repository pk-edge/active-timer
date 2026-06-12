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
GOOGLE_REDIRECT_URI   — must be registered in Google Cloud Console
                        e.g. http://localhost:8000/v1/auth/google/callback
DASHBOARD_URL         — where the browser lands after auth
                        e.g. http://localhost:3002
ALLOWED_EMAIL_DOMAIN  — only this domain is accepted (default: onedge.co)

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
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.secrets import load_google_credentials
from app.core.security import create_admin_token, hash_password_async
from app.db.models import Company, Employee

log = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USER_URL  = "https://www.googleapis.com/oauth2/v3/userinfo"


# ── CSRF state ────────────────────────────────────────────────────────────────

def _make_state() -> str:
    ts    = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    msg   = f"{ts}:{nonce}".encode()
    sig   = hmac.new(settings.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]
    return f"{ts}:{nonce}:{sig}"


def _verify_state(state: str, max_age: int = 600) -> None:
    try:
        ts_str, nonce, sig = state.split(":", 2)
        if time.time() - int(ts_str) > max_age:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth state expired")
        msg      = f"{ts_str}:{nonce}".encode()
        expected = hmac.new(settings.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed OAuth state")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/google")
async def google_oauth_start():
    creds = load_google_credentials()
    if not creds["client_id"]:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google OAuth not configured")

    params = urlencode({
        "client_id":     creds["client_id"],
        "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "online",
        "prompt":        "select_account",
        "state":         _make_state(),
    })
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{params}")


@router.get("/google/callback")
async def google_oauth_callback(
    code:  str = Query(...),
    state: str = Query(...),
    db:    AsyncSession = Depends(get_db),
):
    dash = settings.DASHBOARD_URL

    try:
        _verify_state(state)
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
                "redirect_uri":  settings.GOOGLE_REDIRECT_URI,
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

    # ── Domain enforcement ────────────────────────────────────────────────────
    if not email.endswith(f"@{settings.ALLOWED_EMAIL_DOMAIN}"):
        log.warning("Rejected Google sign-in: %s", email)
        return RedirectResponse(f"{dash}/login?error=domain_not_allowed")

    # ── Find or create employee ───────────────────────────────────────────────
    company_row = await db.execute(select(Company).limit(1))
    company     = company_row.scalar_one_or_none()
    if not company:
        return RedirectResponse(f"{dash}/login?error=company_not_found")

    emp_row  = await db.execute(select(Employee).where(Employee.email == email))
    employee = emp_row.scalar_one_or_none()
    new_user = False

    if employee:
        if employee.google_sub != google_sub:
            employee.google_sub = google_sub
            await db.commit()
        if not employee.is_active or employee.is_deleted:
            return RedirectResponse(f"{dash}/login?error=account_inactive")
    else:
        new_user = True
        employee = Employee(
            company_id=company.id,
            email=email,
            name=name,
            google_sub=google_sub,
            role="admin",
            is_active=True,
            is_approved=True,
        )
        db.add(employee)
        await db.commit()
        await db.refresh(employee)
        log.info("Auto-provisioned Google user: %s", email)

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
