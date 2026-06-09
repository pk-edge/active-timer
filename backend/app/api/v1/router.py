from fastapi import APIRouter

from app.api.v1.endpoints import auth, ingest, devices, screenshots, reports, logs, roles, groups, users, installers, home, audit, notifications, tech_utilization, policy, setup, company

api_router = APIRouter()

api_router.include_router(auth.router,        prefix="/auth",        tags=["auth"])
api_router.include_router(ingest.router,      prefix="/ingest",      tags=["ingest"])
api_router.include_router(devices.router,     prefix="/devices",     tags=["devices"])
api_router.include_router(screenshots.router, prefix="/screenshots", tags=["screenshots"])
api_router.include_router(reports.router,     prefix="/reports",     tags=["reports"])
api_router.include_router(logs.router,        prefix="/logs",        tags=["logs"])
api_router.include_router(roles.router,       prefix="/roles",       tags=["roles"])
api_router.include_router(groups.router,      prefix="/groups",      tags=["groups"])
api_router.include_router(users.router,       prefix="/users",       tags=["users"])
api_router.include_router(installers.router,         prefix="/installers", tags=["installers"])
api_router.include_router(installers.updates_router, prefix="/updates",    tags=["updates"])
api_router.include_router(home.router,        prefix="/home",        tags=["home"])
api_router.include_router(audit.router,         prefix="/audit",         tags=["audit"])
api_router.include_router(notifications.router,   prefix="/notifications",  tags=["notifications"])
api_router.include_router(tech_utilization.router, prefix="/tech",           tags=["tech"])
api_router.include_router(policy.router,           prefix="/policy",         tags=["policy"])
api_router.include_router(setup.router,            prefix="/setup",          tags=["setup"])
api_router.include_router(company.router,          prefix="/company",        tags=["company"])
