"""Runtime config the desktop agent polls (~60s) with its device JWT.

Kept tiny and device-authed so it's cheap to hit often. Serves the effective
headless flag (per-device override, else company default) plus the company's
tracking preferences from the Settings page — this is how an admin's toggles
reach running agents without a reinstall. Older agents ignore unknown fields.

Hot-path design: this endpoint is polled by EVERY device, so its steady state
must touch the DB zero times. Device auth+context comes from the Redis-backed
device-ctx cache (see deps.get_device_ctx) and the company row is cached in
Redis for 30s (evicted immediately on a company-settings change). Only a cache
miss touches the request's lazy DB session.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DeviceCtx, get_db, get_device_ctx
from app.core.RedisCache import cache_get, cache_set

router = APIRouter()

# Plain TTL (fresh == stale): at most one tiny company SELECT per 30s per
# replica, and a settings PATCH deletes the key for instant propagation.
_CACHE_SECS = 30


def _company_cfg_key(cid: str) -> tuple:
    return ("agentcfg", cid)


class AgentConfig(BaseModel):
    headless: bool
    track_urls: bool = True
    track_window_titles: bool = True
    track_keyboard: bool = False
    idle_timeout_secs: int = 300


async def _load_company_config(db: AsyncSession, cid: str) -> dict:
    """Live read of the company's agent-relevant columns (cache-miss path only)."""
    try:
        row = (await db.execute(
            text("SELECT agent_headless, settings FROM companies WHERE id = :cid"),
            {"cid": cid},
        )).fetchone()
        if row:
            return {"agent_headless": bool(row.agent_headless), "settings": row.settings or {}}
    except ProgrammingError:
        # settings column not migrated yet (pre-031) — headless only.
        await db.rollback()
        row = (await db.execute(
            text("SELECT agent_headless FROM companies WHERE id = :cid"),
            {"cid": cid},
        )).fetchone()
        if row:
            return {"agent_headless": bool(row.agent_headless), "settings": {}}
    return {"agent_headless": False, "settings": {}}


@router.get("/config", response_model=AgentConfig)
async def agent_config(
    ctx: DeviceCtx = Depends(get_device_ctx),
    db: AsyncSession = Depends(get_db),   # lazy — no connection touched on cache hits
):
    data, _ = await cache_get(_company_cfg_key(ctx.company_id))
    if data is None:
        data = await _load_company_config(db, ctx.company_id)
        await cache_set(_company_cfg_key(ctx.company_id), data,
                        fresh_secs=_CACHE_SECS, stale_secs=_CACHE_SECS)

    headless_default = bool(data.get("agent_headless"))
    settings: dict = data.get("settings") or {}
    override = ctx.headless_override

    headless = bool(override) if override is not None else headless_default
    idle_mins = settings.get("idle_timeout_mins")
    return AgentConfig(
        headless=headless,
        track_urls=bool(settings.get("track_urls", True)),
        track_window_titles=bool(settings.get("track_window_titles", True)),
        track_keyboard=bool(settings.get("track_keyboard", False)),
        idle_timeout_secs=int(idle_mins) * 60 if isinstance(idle_mins, int) and idle_mins > 0 else 300,
    )
