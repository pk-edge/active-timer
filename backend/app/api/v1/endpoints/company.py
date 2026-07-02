from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.api.deps import get_db, require
from app.core import audit
from app.db.models import Company, Employee

router = APIRouter()

# Company-wide dashboard settings stored in companies.settings (JSONB).
# Keys are whitelisted here — unknown keys from the client are dropped, and
# reads merge over these defaults so the dashboard always sees a full object.
DEFAULT_SETTINGS: dict = {
    # General
    "timezone":            "UTC",
    "work_start":          "09:00",
    "work_end":            "18:00",
    # Tracking (served to agents via /v1/agent/config)
    "track_urls":          True,
    "track_window_titles": True,
    "track_keyboard":      False,
    "idle_timeout_mins":   5,
    # Notifications
    "email_alerts":        True,
    "idle_alert_mins":     30,
    "offline_alert_mins":  15,
    "daily_report":        True,
    # Shared files — max upload size in MB (see endpoints/adminFiles.py)
    "max_file_size_mb":    500,
}

_BOOL_KEYS = {"track_urls", "track_window_titles", "track_keyboard",
              "email_alerts", "daily_report"}
_INT_KEYS  = {"idle_timeout_mins": (0, 240),
              "idle_alert_mins": (0, 1440),
              "offline_alert_mins": (0, 1440),
              "max_file_size_mb": (1, 5120)}
_STR_KEYS  = {"timezone": 64, "work_start": 5, "work_end": 5}


def _clean_settings(raw: dict | None) -> dict:
    """Whitelist + type-check client-supplied settings. Anything else is dropped."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k in _BOOL_KEYS and isinstance(v, bool):
            out[k] = v
        elif k in _INT_KEYS and isinstance(v, int) and not isinstance(v, bool):
            lo, hi = _INT_KEYS[k]
            out[k] = min(max(v, lo), hi)
        elif k in _STR_KEYS and isinstance(v, str):
            out[k] = v[: _STR_KEYS[k]]
    return out


class CompanyInfo(BaseModel):
    name: str
    enrollment_token: str
    agent_headless: bool
    inactive_device_days: int | None  # None = disabled
    settings: dict


class CompanySettingsIn(BaseModel):
    agent_headless: bool
    inactive_device_days: int | None = None
    name: str | None = Field(default=None, max_length=200)
    settings: dict | None = None      # partial update, merged into stored JSON


async def _read_extras(db: AsyncSession, company_id) -> tuple[int | None, dict]:
    """Fetch the deferred/newer columns via raw SQL so a pre-migration DB
    (columns absent) degrades gracefully instead of crashing the endpoint."""
    inactive_days: int | None = None
    stored: dict = {}
    try:
        row = (await db.execute(
            text("SELECT inactive_device_days, settings FROM companies WHERE id = :id"),
            {"id": str(company_id)},
        )).fetchone()
        if row:
            inactive_days = row.inactive_device_days
            stored = row.settings or {}
    except ProgrammingError:
        await db.rollback()
        try:  # migration 027 applied but 031 not yet
            row = (await db.execute(
                text("SELECT inactive_device_days FROM companies WHERE id = :id"),
                {"id": str(company_id)},
            )).fetchone()
            inactive_days = row.inactive_device_days if row else None
        except ProgrammingError:
            await db.rollback()
    return inactive_days, {**DEFAULT_SETTINGS, **_clean_settings(stored)}


@router.get("/info", response_model=CompanyInfo)
async def get_company_info(
    user: Employee = Depends(require("settings", "view")),
    db: AsyncSession = Depends(get_db),
):
    cid = user.company_id
    result = await db.execute(select(Company).where(Company.id == cid))
    company = result.scalar_one()
    # Capture scalars before _read_extras — its pre-migration fallback rolls
    # back the session, which would expire `company` mid-request.
    name, token, headless = company.name, company.enrollment_token, bool(company.agent_headless)

    inactive_days, merged = await _read_extras(db, cid)
    return CompanyInfo(
        name=name,
        enrollment_token=token,
        agent_headless=headless,
        inactive_device_days=inactive_days,
        settings=merged,
    )


@router.patch("/settings", response_model=CompanyInfo)
async def update_company_settings(
    body: CompanySettingsIn,
    user: Employee = Depends(require("settings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Update company-wide settings: agent headless default, inactive-device
    threshold, organization name, and the general/tracking/notification prefs."""
    cid = user.company_id
    result = await db.execute(select(Company).where(Company.id == cid))
    company = result.scalar_one()

    company.agent_headless = bool(body.agent_headless)
    if body.name and body.name.strip():
        company.name = body.name.strip()

    validated_days = (
        body.inactive_device_days
        if (body.inactive_device_days is None or body.inactive_device_days > 0)
        else None
    )
    cleaned = _clean_settings(body.settings)

    inactive_days: int | None = None
    merged = dict(DEFAULT_SETTINGS)
    try:
        # flush() pushes the ORM change to the DB within the open transaction so
        # we can detect a missing column before audit + commit run.
        company.inactive_device_days = validated_days
        new_settings: dict | None = None
        if body.settings is not None:
            stored = _clean_settings(
                (await db.execute(
                    text("SELECT settings FROM companies WHERE id = :id"),
                    {"id": str(cid)},
                )).scalar() or {}
            )
            new_settings = {**stored, **cleaned}
            company.settings = new_settings
        await db.flush()
        inactive_days = validated_days
        if new_settings is None:
            # Not updating settings this call — read current stored values for
            # the response. (Never touch the deferred company.settings attribute
            # unless we assigned it: an unloaded access would trigger IO.)
            new_settings = _clean_settings(
                (await db.execute(
                    text("SELECT settings FROM companies WHERE id = :id"),
                    {"id": str(cid)},
                )).scalar() or {}
            )
        merged = {**DEFAULT_SETTINGS, **new_settings}
    except ProgrammingError:
        # Migration 027/031 not yet applied — roll back and retry with only the
        # columns that exist everywhere.
        await db.rollback()
        result2 = await db.execute(select(Company).where(Company.id == cid))
        company = result2.scalar_one()
        company.agent_headless = bool(body.agent_headless)
        if body.name and body.name.strip():
            company.name = body.name.strip()

    await audit.record(db, user, "company.settings", "company", str(cid),
                       {"agent_headless": company.agent_headless,
                        "inactive_device_days": inactive_days,
                        "name": company.name,
                        "settings": cleaned or None})
    name, token, headless = company.name, company.enrollment_token, bool(company.agent_headless)
    await db.commit()
    return CompanyInfo(
        name=name,
        enrollment_token=token,
        agent_headless=headless,
        inactive_device_days=inactive_days,
        settings=merged,
    )
