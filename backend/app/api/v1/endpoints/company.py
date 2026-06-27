from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db, require
from app.core import audit
from app.db.models import Company, Employee

router = APIRouter()


class CompanyInfo(BaseModel):
    name: str
    enrollment_token: str
    agent_headless: bool


class CompanySettingsIn(BaseModel):
    # Company-wide default agent visibility. True = all agents run headless
    # (no floating widget, no tray icon) unless a device overrides it.
    agent_headless: bool


@router.get("/info", response_model=CompanyInfo)
async def get_company_info(
    user: Employee = Depends(require("settings", "view")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = result.scalar_one()
    return CompanyInfo(
        name=company.name,
        enrollment_token=company.enrollment_token,
        agent_headless=bool(company.agent_headless),
    )


@router.patch("/settings", response_model=CompanyInfo)
async def update_company_settings(
    body: CompanySettingsIn,
    user: Employee = Depends(require("settings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Update company-wide agent settings (currently the headless default)."""
    result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = result.scalar_one()
    company.agent_headless = bool(body.agent_headless)
    await audit.record(db, user, "company.settings", "company", str(company.id),
                       {"agent_headless": company.agent_headless})
    await db.commit()
    return CompanyInfo(
        name=company.name,
        enrollment_token=company.enrollment_token,
        agent_headless=bool(company.agent_headless),
    )
