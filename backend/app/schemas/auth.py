from typing import Optional
from pydantic import BaseModel, EmailStr


class EnrollRequest(BaseModel):
    enrollment_token: str
    hostname:         str
    platform:         str
    agent_version:    Optional[str] = None
    # Plain str, NOT EmailStr — a malformed detected email must never 422 the
    # enrollment; provisioning quietly ignores unusable values.
    employee_email:   Optional[str] = None


class EnrollResponse(BaseModel):
    device_id: str
    jwt_token: str


class AdminLoginRequest(BaseModel):
    email:    EmailStr
    password: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    role:         str
    name:         str


# ── Super-Admin impersonation ────────────────────────────────────────────────

class ImpersonateRequest(BaseModel):
    user_id: str


class ImpersonationTargetOut(BaseModel):
    id:        str
    name:      str
    email:     str
    role_name: Optional[str] = None


class ImpersonateResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    expires_in:   int                      # seconds until the session self-expires
    target:       ImpersonationTargetOut


class ExitImpersonationResponse(BaseModel):
    """A freshly minted ordinary admin token — the original is never handed back,
    it is re-issued server-side after re-verifying the admin."""
    access_token: str
    token_type:   str = "bearer"
    role:         str
    name:         str
