"""Runtime config the desktop agent polls (~60s) with its device JWT.

Kept tiny and device-authed so it's cheap to hit often. Currently just the
effective headless flag: the per-device override if set, else the company-wide
default. This is how an admin's "make agents headless/visible" toggle reaches
running agents without a reinstall.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_device
from app.db.models import Company, Device

router = APIRouter()


class AgentConfig(BaseModel):
    headless: bool


@router.get("/config", response_model=AgentConfig)
async def agent_config(
    device: Device = Depends(get_current_device),
    db: AsyncSession = Depends(get_db),
):
    if device.headless_override is not None:
        headless = bool(device.headless_override)
    else:
        company = (await db.execute(
            select(Company).where(Company.id == device.company_id)
        )).scalar_one()
        headless = bool(company.agent_headless)
    return AgentConfig(headless=headless)
