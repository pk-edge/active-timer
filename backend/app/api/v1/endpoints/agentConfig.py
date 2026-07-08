"""Runtime config the desktop agent polls (~60s) with its device JWT.

Kept tiny and device-authed so it's cheap to hit often. Serves the effective
headless flag (per-device override, else company default) plus the company's
tracking preferences from the Settings page — this is how an admin's toggles
reach running agents without a reinstall. Older agents ignore unknown fields.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_device
from app.db.models import Device

router = APIRouter()


class AgentConfig(BaseModel):
    headless: bool
    track_urls: bool = True
    track_window_titles: bool = True
    track_keyboard: bool = False
    idle_timeout_secs: int = 300


@router.get("/config", response_model=AgentConfig)
async def agent_config(
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    # Capture scalars before any rollback below can expire the ORM instance.
    cid = str(device.company_id)
    override = device.headless_override

    headless_default = False
    settings: dict = {}
    try:
        row = (await db.execute(
            text("SELECT agent_headless, settings FROM companies WHERE id = :cid"),
            {"cid": cid},
        )).fetchone()
        if row:
            headless_default = bool(row.agent_headless)
            settings = row.settings or {}
    except ProgrammingError:
        # settings column not migrated yet (pre-031) — headless only.
        await db.rollback()
        row = (await db.execute(
            text("SELECT agent_headless FROM companies WHERE id = :cid"),
            {"cid": cid},
        )).fetchone()
        headless_default = bool(row.agent_headless) if row else False

    headless = bool(override) if override is not None else headless_default
    idle_mins = settings.get("idle_timeout_mins")
    return AgentConfig(
        headless=headless,
        track_urls=bool(settings.get("track_urls", True)),
        track_window_titles=bool(settings.get("track_window_titles", True)),
        track_keyboard=bool(settings.get("track_keyboard", False)),
        idle_timeout_secs=int(idle_mins) * 60 if isinstance(idle_mins, int) and idle_mins > 0 else 300,
    )
