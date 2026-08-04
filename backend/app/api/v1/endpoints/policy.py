"""
Policy endpoints — blocked URL management.

Admin endpoints (require policy:edit):
  POST   /v1/policy/blocked-urls
  PATCH  /v1/policy/blocked-urls/{id}
  DELETE /v1/policy/blocked-urls/{id}

Read endpoints:
  GET    /v1/policy/blocked-urls   (admin: full list — require policy:view)
  GET    /v1/policy/blocked-urls   (device JWT: filtered to relevant rules)
"""
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from sqlalchemy import text
from app.api.deps import DeviceCtx, get_db, get_device_ctx, require, get_current_admin
from app.core import audit
from app.core.RedisCache import cache_delete, cache_get, cache_set
from app.db.models import BlockedUrl, Employee
from app.schemas.policy import BlockedUrlOut, BlockedUrlCreate, BlockedUrlUpdate, AgentPolicyItem

router = APIRouter()

# Agents poll this endpoint, so its steady state must be DB-free: the company's
# full active rule set is cached in Redis (rule lists are small) and filtered
# per device in Python — identical payload to the old per-device SQL filter.
# Mutations evict the key, so 30s is only the cross-replica refresh cadence.
_RULES_CACHE_SECS = 30


def _rules_cache_key(cid: str) -> tuple:
    return ("policyrules", cid)


async def _load_company_rules(db: AsyncSession, cid: str) -> list[dict]:
    result = await db.execute(
        select(BlockedUrl).where(
            BlockedUrl.company_id == cid,
            BlockedUrl.is_active  == True,   # noqa: E712
            BlockedUrl.is_deleted == False,  # noqa: E712
        )
    )
    return [
        {
            "id": str(r.id),
            "pattern": r.pattern,
            "target_type": r.target_type,
            "device_id": str(r.device_id) if r.device_id else None,
            "employee_id": str(r.employee_id) if r.employee_id else None,
        }
        for r in result.scalars().all()
    ]


# ── Agent endpoint — device JWT ───────────────────────────────────────────────

@router.get("/blocked-urls", response_model=List[AgentPolicyItem])
async def get_policy_for_device(
    ctx: DeviceCtx = Depends(get_device_ctx),
    db: AsyncSession = Depends(get_db),   # lazy — no connection touched on cache hits
):
    """
    Called by the agent. Returns all active blocked-URL rules that apply to
    this device — either company-wide ('all') or targeted at this specific device
    or its assigned employee.
    """
    rules, _ = await cache_get(_rules_cache_key(ctx.company_id))
    if rules is None:
        rules = await _load_company_rules(db, ctx.company_id)
        await cache_set(_rules_cache_key(ctx.company_id), rules,
                        fresh_secs=_RULES_CACHE_SECS, stale_secs=_RULES_CACHE_SECS)

    return [
        AgentPolicyItem(id=r["id"], pattern=r["pattern"], target_type=r["target_type"])
        for r in rules
        if (
            r["target_type"] == "all"
            or (r["device_id"] and r["device_id"] == ctx.device_id)
            or (r["employee_id"] and ctx.employee_id and r["employee_id"] == ctx.employee_id)
        )
    ]


# ── Admin list ────────────────────────────────────────────────────────────────

@router.get("/detected-urls")
async def detected_urls(
    limit: int = Query(default=300, ge=1, le=1000),
    admin: Employee = Depends(require("policy", "view")),
    db: AsyncSession = Depends(get_db),
):
    """URLs seen from company devices (last 30 days), minus already-blocked patterns.

    Two-step hostname extraction avoids SQLAlchemy misreading the regex non-capturing
    group (?:...) as a bind parameter — we use [.] instead of \\. in REGEXP_REPLACE.
    """
    result = await db.execute(text("""
        WITH raw AS (
            SELECT
                LOWER(COALESCE(
                    substring(ae.url FROM '^https?://([^/?#]+)'),
                    ''
                )) AS raw_host
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id  = :cid
              AND ae.event_type = 'url_visit'
              AND ae.url IS NOT NULL
              AND ae.timestamp >= NOW() - interval '30 days'
              AND ae.url NOT LIKE '%localhost%'
              AND ae.url NOT LIKE '%127.0.0.1%'
        ),
        hosts AS (
            SELECT
                REGEXP_REPLACE(raw_host, '^www[.]', '') AS host,
                COUNT(*)::int AS visits
            FROM raw
            WHERE raw_host <> ''
            GROUP BY REGEXP_REPLACE(raw_host, '^www[.]', '')
        )
        SELECT h.host, h.visits
        FROM hosts h
        WHERE h.host IS NOT NULL
          AND h.host <> ''
          AND NOT EXISTS (
              SELECT 1 FROM blocked_urls bu
              WHERE bu.company_id = :cid
                AND bu.is_active  = TRUE
                AND bu.pattern    = h.host
          )
        ORDER BY h.visits DESC
        LIMIT :limit
    """), {"cid": str(admin.company_id), "limit": limit})
    return [{"host": r.host, "visits": r.visits} for r in result.fetchall()]


@router.get("/blocked-urls/admin", response_model=List[BlockedUrlOut])
async def list_blocked_urls(
    is_active: bool | None = Query(default=None),
    admin: Employee = Depends(require("policy", "view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(BlockedUrl).where(
        BlockedUrl.company_id == admin.company_id,
        BlockedUrl.is_deleted == False,  # noqa: E712
    )
    if is_active is not None:
        q = q.where(BlockedUrl.is_active == is_active)
    result = await db.execute(q.order_by(BlockedUrl.created_at.desc()))
    return result.scalars().all()


# ── Admin CRUD ────────────────────────────────────────────────────────────────

@router.post("/blocked-urls", response_model=BlockedUrlOut, status_code=201)
async def create_blocked_url(
    body: BlockedUrlCreate,
    admin: Employee = Depends(require("policy", "edit")),
    db: AsyncSession = Depends(get_db),
):
    rule = BlockedUrl(
        company_id=admin.company_id,
        pattern=body.pattern.strip().lower(),
        target_type=body.target_type,
        device_id=body.device_id,
        employee_id=body.employee_id,
        notes=body.notes,
        created_by=admin.id,
    )
    db.add(rule)
    await db.flush()
    await audit.record(db, admin, "policy.create", "policy", None,
                       {"pattern": rule.pattern, "target_type": rule.target_type})
    await db.commit()
    await cache_delete(_rules_cache_key(str(admin.company_id)))
    await db.refresh(rule)
    return rule


@router.patch("/blocked-urls/{rule_id}", response_model=BlockedUrlOut)
async def update_blocked_url(
    rule_id: str,
    body: BlockedUrlUpdate,
    admin: Employee = Depends(require("policy", "edit")),
    db: AsyncSession = Depends(get_db),
):
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")
    result = await db.execute(
        select(BlockedUrl).where(BlockedUrl.id == rid, BlockedUrl.company_id == admin.company_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(rule, field, val)
    await audit.record(db, admin, "policy.update", "policy", rule_id,
                       {"pattern": rule.pattern})
    await db.commit()
    await cache_delete(_rules_cache_key(str(admin.company_id)))
    await db.refresh(rule)
    return rule


@router.delete("/blocked-urls/{rule_id}", status_code=204)
async def delete_blocked_url(
    rule_id: str,
    admin: Employee = Depends(require("policy", "edit")),
    db: AsyncSession = Depends(get_db),
):
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")
    result = await db.execute(
        select(BlockedUrl).where(BlockedUrl.id == rid, BlockedUrl.company_id == admin.company_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.is_deleted = True
    await audit.record(db, admin, "policy.delete", "policy", rule_id,
                       {"pattern": rule.pattern})
    await db.commit()
    await cache_delete(_rules_cache_key(str(admin.company_id)))
