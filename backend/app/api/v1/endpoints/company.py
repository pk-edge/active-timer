from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db, require
from app.db.models import Company, Employee

router = APIRouter()


class CompanyInfo(BaseModel):
    name: str
    enrollment_token: str


@router.get("/info", response_model=CompanyInfo)
async def get_company_info(
    user: Employee = Depends(require("settings", "view")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Company).where(Company.id == user.company_id))
    company = result.scalar_one()
    return CompanyInfo(name=company.name, enrollment_token=company.enrollment_token)
