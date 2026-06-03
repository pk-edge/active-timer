from fastapi import APIRouter

from app.api.v1.endpoints import auth, ingest, devices, screenshots, reports, logs

api_router = APIRouter()

api_router.include_router(auth.router,        prefix="/auth",        tags=["auth"])
api_router.include_router(ingest.router,      prefix="/ingest",      tags=["ingest"])
api_router.include_router(devices.router,     prefix="/devices",     tags=["devices"])
api_router.include_router(screenshots.router, prefix="/screenshots", tags=["screenshots"])
api_router.include_router(reports.router,     prefix="/reports",     tags=["reports"])
api_router.include_router(logs.router,        prefix="/logs",        tags=["logs"])
