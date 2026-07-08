import math
import uuid
from datetime import date as _date, timedelta as _td
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text

from app.api.deps import get_db, require
from app.core.group_tree import descendant_group_ids, devices_for_groups
from app.core import audit
from app.core.app_normalizer import normalize_app_name
from app.core.scoping import visible_device_ids, ALL
from app.core.RedisCache import cache_get, cache_set, FRESH_ANALYTICS, STALE_ANALYTICS, FRESH_DETAIL, STALE_DETAIL
from app.db.models import AppCategory, AppCategoryRule, Employee, Group
from app.schemas.tech import (
    CategoryOut, CategoryRuleOut, CategoryCreate, CategoryUpdate, RuleCreate, CategoriesPageOut,
    CategoryAllocationItem, AppTrendsOut, AppTrendItem, TechSummaryOut,
    AppDetailItem, AdoptionOut,
    WebsiteItem, WebsiteListOut, WebsiteDetailOut, WebsiteDetailTitle,
    WebsiteDetailSubpage, WebsiteDetailUser, WebsiteDetailUsagePoint,
    AppUsageItem, AppUsageListOut, AppUsageDetailOut, AppUsageDetailTitle,
    AppUsageDetailUser, AppUsageDetailPoint,
    CategoryUsageItem, CategoryUsageListOut, CategoryUsageDetailOut,
    CategoryDetailActivity, CategoryDetailUser,
)

router = APIRouter()

DEFAULT_CATEGORIES = [
    {"name": "Productivity",    "color": "#3b82f6"},
    {"name": "Communication",   "color": "#22c55e"},
    {"name": "Development",     "color": "#8b5cf6"},
    {"name": "Browser",         "color": "#f59e0b"},
    {"name": "Meetings",        "color": "#06b6d4"},
    {"name": "Media",           "color": "#ec4899"},
    {"name": "Admin & IT",      "color": "#64748b"},
]

DEFAULT_RULES: dict[str, list[str]] = {
    "Productivity":  ["Microsoft Excel", "Microsoft Word", "Microsoft PowerPoint", "Notion", "Google Docs", "Google Sheets", "Google Slides", "LibreOffice Writer", "LibreOffice Calc"],
    "Communication": ["Slack", "Microsoft Teams", "Discord", "Telegram", "WhatsApp", "Signal", "Mail", "Outlook", "Thunderbird", "Gmail"],
    "Development":   ["Code", "cursor", "Visual Studio Code", "IntelliJ IDEA", "PyCharm", "WebStorm", "Xcode", "Android Studio", "Terminal", "iTerm2", "Git", "GitHub Desktop", "Postman", "Insomnia"],
    "Browser":       ["Chrome", "Google Chrome", "Firefox", "Safari", "Microsoft Edge", "Opera", "Brave Browser", "Arc"],
    "Meetings":      ["Zoom", "zoom.us", "Microsoft Teams", "Google Meet", "Webex", "Loom", "FaceTime"],
    "Media":         ["YouTube", "Spotify", "VLC", "QuickTime Player", "Netflix", "Figma", "Adobe Photoshop", "Adobe Illustrator"],
    "Admin & IT":    ["Finder", "Explorer", "System Preferences", "System Settings", "Task Manager", "Activity Monitor", "1Password", "Bitwarden"],
}


# Companies whose system categories were already verified/seeded by this process —
# skips one SELECT on every analytics request after the first.
_default_cats_ensured: set[uuid.UUID] = set()


async def _ensure_default_categories(company_id: uuid.UUID, db: AsyncSession) -> None:
    if company_id in _default_cats_ensured:
        return

    existing = await db.execute(
        select(AppCategory.id).where(AppCategory.company_id == company_id, AppCategory.is_system == True)
        .limit(1)
    )
    if existing.first():
        _default_cats_ensured.add(company_id)
        return

    # One query for all existing patterns instead of one per default rule.
    rules_res = await db.execute(
        select(AppCategoryRule.app_pattern).where(AppCategoryRule.company_id == company_id)
    )
    existing_patterns = {p for (p,) in rules_res.all()}

    # Pre-generate UUIDs in Python so we can build categories + rules in one
    # batch and commit with a single round-trip — no per-category flush needed.
    cats_to_add: list[AppCategory] = []
    rules_to_add: list[AppCategoryRule] = []
    for cat_def in DEFAULT_CATEGORIES:
        cat_id = uuid.uuid4()
        cats_to_add.append(AppCategory(
            id=cat_id,
            company_id=company_id,
            name=cat_def["name"],
            color=cat_def["color"],
            is_system=True,
        ))
        for raw in DEFAULT_RULES.get(cat_def["name"], []):
            pattern = normalize_app_name(raw)
            if pattern not in existing_patterns:
                existing_patterns.add(pattern)
                rules_to_add.append(AppCategoryRule(
                    category_id=cat_id,
                    company_id=company_id,
                    app_pattern=pattern,
                ))

    db.add_all(cats_to_add)
    db.add_all(rules_to_add)
    await db.commit()
    _default_cats_ensured.add(company_id)


async def _scope_clause(admin: Employee, db: AsyncSession) -> tuple[str, dict]:
    ids = await visible_device_ids(admin, db)
    if ids is ALL:
        return "", {}
    return " AND ae.device_id = ANY(:vids)", {"vids": [str(i) for i in ids]}


def _resolve_dates(days: int, since_date: Optional[str], until_date: Optional[str]) -> tuple[_date, _date]:
    """Return (since_dt, until_dt) as date objects for asyncpg CAST(:x AS date) compatibility."""
    if since_date and until_date:
        return _date.fromisoformat(since_date), _date.fromisoformat(until_date)
    today = _date.today()
    return today - _td(days=days - 1), today


def _prev_dates(since_dt: _date, until_dt: _date) -> tuple[_date, _date]:
    """Return the immediately preceding period of equal length for comparison."""
    dur = (until_dt - since_dt).days + 1
    p_until = since_dt - _td(days=1)
    p_since = p_until - _td(days=dur - 1)
    return p_since, p_until


# ── Category CRUD ─────────────────────────────────────────────────────────────

@router.get("/categories", response_model=CategoriesPageOut)
async def list_categories(
    page:      int            = Query(default=1,  ge=1),
    per_page:  int            = Query(default=10, ge=1, le=200),
    is_system: Optional[bool] = Query(default=None),
    admin: Employee = Depends(require("categories", "view")),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_default_categories(admin.company_id, db)

    base_filter = [
        AppCategory.company_id == admin.company_id,
        AppCategory.is_deleted == False,  # noqa: E712
    ]
    if is_system is not None:
        base_filter.append(AppCategory.is_system == is_system)

    total: int = (
        await db.execute(select(func.count(AppCategory.id)).where(*base_filter))
    ).scalar_one()

    cats = (
        await db.execute(
            select(AppCategory)
            .where(*base_filter)
            .order_by(AppCategory.is_system.desc(), AppCategory.name)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    # Fetch rules only for this page's categories — avoids loading the entire
    # rule table when paginating through a large category list.
    cat_ids = [c.id for c in cats]
    rules_by_cat: dict = {}
    if cat_ids:
        for r in (
            await db.execute(
                select(AppCategoryRule).where(AppCategoryRule.category_id.in_(cat_ids))
            )
        ).scalars().all():
            rules_by_cat.setdefault(r.category_id, []).append(r)

    items = [
        CategoryOut(
            id=c.id,
            name=c.name,
            color=c.color,
            description=c.description,
            is_system=c.is_system,
            rule_count=len(rules_by_cat.get(c.id, [])),
            rules=[
                CategoryRuleOut(id=r.id, app_pattern=r.app_pattern, created_at=r.created_at)
                for r in rules_by_cat.get(c.id, [])
            ],
        )
        for c in cats
    ]

    return CategoriesPageOut(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=max(1, math.ceil(total / per_page)),
    )


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(
    body: CategoryCreate,
    admin: Employee = Depends(require("categories", "edit")),
    db: AsyncSession = Depends(get_db),
):
    cat = AppCategory(
        company_id=admin.company_id,
        name=body.name,
        color=body.color,
        description=body.description,
        is_system=False,
    )
    db.add(cat)
    await db.flush()
    await audit.record(db, admin, "category.create", "category", str(cat.id), {"name": cat.name})
    await db.commit()
    await db.refresh(cat)
    return CategoryOut(id=cat.id, name=cat.name, color=cat.color,
                       description=cat.description, is_system=cat.is_system,
                       rule_count=0, rules=[])


@router.put("/categories/{cat_id}", response_model=CategoryOut)
async def update_category(
    cat_id: str,
    body: CategoryUpdate,
    admin: Employee = Depends(require("categories", "edit")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(cat_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")
    result = await db.execute(
        select(AppCategory).where(AppCategory.id == cid, AppCategory.company_id == admin.company_id)
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if body.name is not None:
        cat.name = body.name
    if body.color is not None:
        cat.color = body.color
    if body.description is not None:
        cat.description = body.description
    await audit.record(db, admin, "category.update", "category", cat_id, {"name": cat.name})
    await db.commit()
    await db.refresh(cat)
    rules_res = await db.execute(select(AppCategoryRule).where(AppCategoryRule.category_id == cat.id))
    rules = rules_res.scalars().all()
    return CategoryOut(
        id=cat.id, name=cat.name, color=cat.color, description=cat.description,
        is_system=cat.is_system, rule_count=len(rules),
        rules=[CategoryRuleOut(id=r.id, app_pattern=r.app_pattern, created_at=r.created_at) for r in rules],
    )


@router.delete("/categories/{cat_id}", status_code=204)
async def delete_category(
    cat_id: str,
    admin: Employee = Depends(require("categories", "edit")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(cat_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")
    result = await db.execute(
        select(AppCategory).where(AppCategory.id == cid, AppCategory.company_id == admin.company_id)
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if cat.is_system:
        raise HTTPException(status_code=400, detail="Cannot delete system category")
    cat.is_deleted = True
    await audit.record(db, admin, "category.delete", "category", cat_id, {"name": cat.name})
    await db.commit()


# ── Category Rules CRUD ───────────────────────────────────────────────────────

@router.post("/categories/{cat_id}/rules", response_model=CategoryRuleOut, status_code=201)
async def add_rule(
    cat_id: str,
    body: RuleCreate,
    admin: Employee = Depends(require("categories", "edit")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cid = uuid.UUID(cat_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")
    result = await db.execute(
        select(AppCategory).where(AppCategory.id == cid, AppCategory.company_id == admin.company_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Category not found")

    # Store the canonical form so the rule matches normalized events (e.g. a
    # "Chrome" rule is saved as "Google Chrome", matching normalize_app_name).
    pattern = normalize_app_name(body.app_pattern)

    existing = await db.execute(
        select(AppCategoryRule).where(
            AppCategoryRule.company_id == admin.company_id,
            AppCategoryRule.app_pattern == pattern,
        )
    )
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail="App pattern already assigned to a category")

    rule = AppCategoryRule(category_id=cid, company_id=admin.company_id, app_pattern=pattern)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return CategoryRuleOut(id=rule.id, app_pattern=rule.app_pattern, created_at=rule.created_at)


@router.delete("/categories/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    admin: Employee = Depends(require("categories", "edit")),
    db: AsyncSession = Depends(get_db),
):
    try:
        rid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")
    result = await db.execute(
        select(AppCategoryRule).where(
            AppCategoryRule.id == rid,
            AppCategoryRule.company_id == admin.company_id,
        )
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    await db.commit()


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/utilization/summary", response_model=TechSummaryOut)
async def tech_summary(
    days:        int           = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    group_id:    Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    await _ensure_default_categories(admin.company_id, db)
    sc, sp = await _scope_clause(admin, db)
    gc, gp = await _group_clause(group_id, admin.company_id, db)
    ec, ep = _employee_clause(employee_id)
    clause, sparams = sc + gc + ec, {**sp, **gp, **ep}

    _scope_fp = "all" if not clause else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:summary", str(admin.company_id), _scope_fp, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admin with no group/employee filter reads from pg cache.
    if not clause:
        from app.core import tech_cache
        cached = await tech_cache.summary_from_cache(db, admin.company_id, since_dt, until_dt)
        if cached is not None:
            totals, users, top_cat, top_app = cached
            _result = TechSummaryOut(
                total_hours=round((totals.total_seconds or 0) / 3600, 1),
                total_apps=int(totals.total_apps or 0),
                total_users=int(users.total_users or 0) if users else 0,
                top_category=top_cat.category_name if top_cat else None,
                top_category_color=top_cat.color if top_cat else "#6366f1",
                top_app=top_app.app_name if top_app else None,
            )
            await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _result

    # Fallback: live aggregation (filtered requests or cache miss/stale).
    total_res = await db.execute(text(f"""
        SELECT
            COALESCE(SUM(ae.duration), 0)                    AS total_seconds,
            COUNT(DISTINCT normalize_app_name(ae.app_name)) AS total_apps,
            COUNT(DISTINCT d.employee_id)                   AS total_users
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          {clause}
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, **sparams})
    row = total_res.fetchone()

    cat_res = await db.execute(text(f"""
        SELECT c.name, c.color, SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name) AND r.company_id = d.company_id
        JOIN app_categories c ON c.id = r.category_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          {clause}
        GROUP BY c.name, c.color
        ORDER BY seconds DESC
        LIMIT 1
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, **sparams})
    top_cat = cat_res.fetchone()

    app_res = await db.execute(text(f"""
        SELECT normalize_app_name(ae.app_name) AS app_name,
               SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          {clause}
        GROUP BY normalize_app_name(ae.app_name)
        ORDER BY seconds DESC
        LIMIT 1
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, **sparams})
    top_app = app_res.fetchone()

    _result = TechSummaryOut(
        total_hours=round((row.total_seconds or 0) / 3600, 1),
        total_apps=int(row.total_apps or 0),
        total_users=int(row.total_users or 0),
        top_category=top_cat.name if top_cat else None,
        top_category_color=top_cat.color if top_cat else "#6366f1",
        top_app=top_app.app_name if top_app else None,
    )
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


@router.get("/utilization/category-allocation", response_model=List[CategoryAllocationItem])
async def category_allocation(
    days:         int           = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    compare_days: int           = Query(default=30, ge=1, le=365),
    group_id:     Optional[str] = Query(default=None),
    employee_id:  Optional[str] = Query(default=None),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    prev_since_dt, prev_until_dt = _prev_dates(since_dt, until_dt)
    await _ensure_default_categories(admin.company_id, db)
    sc, sp = await _scope_clause(admin, db)
    gc, gp = await _group_clause(group_id, admin.company_id, db)
    ec, ep = _employee_clause(employee_id)
    clause, sparams = sc + gc + ec, {**sp, **gp, **ep}

    _scope_fp = "all" if not clause else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:catalloc", str(admin.company_id), _scope_fp, str(since_dt), str(until_dt), compare_days)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admin with no group/employee filter reads from pg cache.
    if not clause:
        from app.core import tech_cache
        cached = await tech_cache.category_allocation_from_cache(
            db, admin.company_id, since_dt, until_dt, prev_since_dt, prev_until_dt
        )
        if cached is not None:
            current_rows_raw, prev_rows = cached
            result = []
            for cur in current_rows_raw:
                prev = prev_rows.get(cur.category_name)
                cur_pct   = float(cur.pct or 0)
                prev_pct  = float(prev.pct if prev else 0)
                cur_users = int(cur.users or 0)
                prev_users = int(prev.users if prev else 0)
                result.append(CategoryAllocationItem(
                    category_id=cur.category_id,
                    category_name=cur.category_name,
                    color=cur.color,
                    seconds=int(cur.seconds or 0),
                    pct=cur_pct,
                    prev_seconds=int(prev.seconds if prev else 0),
                    prev_pct=prev_pct,
                    pct_change=round(cur_pct - prev_pct, 1),
                    hours=round((cur.seconds or 0) / 3600, 1),
                    user_change=cur_users - prev_users,
                ))
            await cache_set(_ckey, [i.model_dump() for i in result], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return result

    # Fallback: live aggregation (filtered requests or cache miss/stale).
    current_res = await db.execute(text(f"""
        WITH categorized AS (
            SELECT
                c.id::text AS category_id,
                c.name     AS category_name,
                c.color,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name) AND r.company_id = d.company_id
            JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {clause}
            GROUP BY c.id, c.name, c.color
        ),
        uncategorized AS (
            SELECT
                NULL::text AS category_id,
                'Uncategorized' AS category_name,
                '#94a3b8' AS color,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id = d.company_id AND r2.app_pattern = normalize_app_name(ae.app_name)
              )
              {clause}
            GROUP BY category_name, color
        ),
        combined AS (SELECT * FROM categorized UNION ALL SELECT * FROM uncategorized),
        total AS (SELECT SUM(seconds) AS t FROM combined)
        SELECT c.*, ROUND(100.0 * c.seconds / NULLIF(t.t, 0), 1) AS pct
        FROM combined c, total t
        ORDER BY seconds DESC
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, **sparams})
    current_rows = {r.category_name: r for r in current_res.fetchall()}

    prev_res = await db.execute(text(f"""
        WITH categorized AS (
            SELECT
                c.name AS category_name,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name) AND r.company_id = d.company_id
            JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:prev_since_dt AS date) AND ae.timestamp < (CAST(:prev_until_dt AS date) + INTERVAL '1 day')
              {clause}
            GROUP BY c.name
        ),
        uncategorized AS (
            SELECT
                'Uncategorized' AS category_name,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:prev_since_dt AS date) AND ae.timestamp < (CAST(:prev_until_dt AS date) + INTERVAL '1 day')
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id = d.company_id AND r2.app_pattern = normalize_app_name(ae.app_name)
              )
              {clause}
            GROUP BY category_name
        ),
        combined AS (SELECT * FROM categorized UNION ALL SELECT * FROM uncategorized),
        total AS (SELECT SUM(seconds) AS t FROM combined)
        SELECT c.category_name,
               c.seconds,
               c.users,
               ROUND(100.0 * c.seconds / NULLIF(t.t, 0), 1) AS pct
        FROM combined c, total t
    """), {"company_id": str(admin.company_id), "prev_since_dt": prev_since_dt, "prev_until_dt": prev_until_dt, **sparams})
    prev_rows = {r.category_name: r for r in prev_res.fetchall()}

    result = []
    for name, cur in current_rows.items():
        prev = prev_rows.get(name)
        cur_pct   = float(cur.pct or 0)
        prev_pct  = float(prev.pct if prev else 0)
        cur_users = int(cur.users or 0)
        prev_users = int(prev.users if prev else 0)
        result.append(CategoryAllocationItem(
            category_id=cur.category_id,
            category_name=name,
            color=cur.color,
            seconds=int(cur.seconds or 0),
            pct=cur_pct,
            prev_seconds=int(prev.seconds if prev else 0),
            prev_pct=prev_pct,
            pct_change=round(cur_pct - prev_pct, 1),
            hours=round((cur.seconds or 0) / 3600, 1),
            user_change=cur_users - prev_users,
        ))
    await cache_set(_ckey, [i.model_dump() for i in result], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return result


@router.get("/utilization/app-trends", response_model=AppTrendsOut)
async def app_trends(
    days:         int = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    compare_days: int = Query(default=30, ge=1, le=365),
    limit:        int = Query(default=10, ge=1, le=50),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    prev_since_dt, prev_until_dt = _prev_dates(since_dt, until_dt)
    await _ensure_default_categories(admin.company_id, db)
    clause, sparams = await _scope_clause(admin, db)

    _scope_fp = "all" if not clause else str(admin.id)
    _ckey = ("tech:apptrends", str(admin.company_id), _scope_fp, str(since_dt), str(until_dt), compare_days, limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    base_params = {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt,
                   "prev_since_dt": prev_since_dt, "prev_until_dt": prev_until_dt, **sparams}

    current_res = await db.execute(text(f"""
        WITH app_data AS (
            SELECT
                normalize_app_name(ae.app_name) AS app_name,
                COALESCE(c.id::text, NULL) AS category_id,
                COALESCE(c.name, 'Uncategorized') AS category_name,
                COALESCE(c.color, '#94a3b8') AS color,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            LEFT JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name) AND r.company_id = d.company_id
            LEFT JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {clause}
            GROUP BY normalize_app_name(ae.app_name), c.id, c.name, c.color
        ),
        total AS (SELECT SUM(seconds) AS t FROM app_data)
        SELECT a.*, ROUND(100.0 * a.seconds / NULLIF(t.t, 0), 2) AS pct
        FROM app_data a, total t
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, **sparams})
    cur_rows = {r.app_name: r for r in current_res.fetchall()}

    prev_res = await db.execute(text(f"""
        WITH app_data AS (
            SELECT
                normalize_app_name(ae.app_name) AS app_name,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:prev_since_dt AS date) AND ae.timestamp < (CAST(:prev_until_dt AS date) + INTERVAL '1 day')
              {clause}
            GROUP BY normalize_app_name(ae.app_name)
        ),
        total AS (SELECT SUM(seconds) AS t FROM app_data)
        SELECT a.*, ROUND(100.0 * a.seconds / NULLIF(t.t, 0), 2) AS pct
        FROM app_data a, total t
    """), base_params)
    prev_rows = {r.app_name: r for r in prev_res.fetchall()}

    items = []
    for app_name, cur in cur_rows.items():
        prev = prev_rows.get(app_name)
        cur_pct  = float(cur.pct or 0)
        prev_pct = float(prev.pct if prev else 0)
        items.append(AppTrendItem(
            app_name=app_name,
            category_name=cur.category_name,
            color=cur.color,
            seconds=int(cur.seconds or 0),
            prev_seconds=int(prev.seconds if prev else 0),
            pct=cur_pct,
            prev_pct=prev_pct,
            pct_change=round(cur_pct - prev_pct, 2),
            hours=round((cur.seconds or 0) / 3600, 1),
            user_change=int(cur.users or 0) - int(prev.users if prev else 0),
        ))

    items.sort(key=lambda x: x.pct_change, reverse=True)
    trending_up   = [i for i in items if i.pct_change > 0][:limit]
    trending_down = sorted([i for i in items if i.pct_change < 0], key=lambda x: x.pct_change)[:limit]

    _result = AppTrendsOut(trending_up=trending_up, trending_down=trending_down)
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


@router.get("/utilization/apps", response_model=List[AppDetailItem])
async def app_list(
    days:        int           = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    category_id: Optional[str] = Query(default=None),
    group_id:    Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    limit:       int           = Query(default=50, ge=1, le=200),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    await _ensure_default_categories(admin.company_id, db)
    sc, sp = await _scope_clause(admin, db)
    gc, gp = await _group_clause(group_id, admin.company_id, db)
    ec, ep = _employee_clause(employee_id)
    clause, sparams = sc + gc + ec, {**sp, **gp, **ep}

    cat_filter = ""
    if category_id:
        try:
            uuid.UUID(category_id)
            cat_filter = " AND c.id = :cat_id"
            sparams["cat_id"] = category_id
        except ValueError:
            pass

    _scope_fp = "all" if not clause else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:applist", str(admin.company_id), _scope_fp, category_id or "", str(since_dt), str(until_dt), limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admin with no group/employee/category filter reads from pg cache.
    if not clause and not category_id:
        from app.core import tech_cache
        cached_rows = await tech_cache.app_list_from_cache(
            db, admin.company_id, since_dt, until_dt, limit
        )
        if cached_rows is not None:
            _items = [
                AppDetailItem(
                    app_name=r.app_name,
                    category_id=r.category_id,
                    category_name=r.category_name,
                    color=r.color,
                    seconds=int(r.seconds or 0),
                    hours=round((r.seconds or 0) / 3600, 1),
                    pct=float(r.pct or 0),
                    user_count=int(r.user_count or 0),
                )
                for r in cached_rows
            ]
            await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _items

    # Fallback: live aggregation (filtered requests or cache miss/stale).
    result = await db.execute(text(f"""
        WITH app_data AS (
            SELECT
                normalize_app_name(ae.app_name) AS app_name,
                COALESCE(c.id::text, NULL) AS category_id,
                COALESCE(c.name, 'Uncategorized') AS category_name,
                COALESCE(c.color, '#94a3b8') AS color,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS user_count
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            LEFT JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name) AND r.company_id = d.company_id
            LEFT JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {cat_filter}
              {clause}
            GROUP BY normalize_app_name(ae.app_name), c.id, c.name, c.color
        ),
        total AS (SELECT SUM(seconds) AS t FROM app_data)
        SELECT a.*, ROUND(100.0 * a.seconds / NULLIF(t.t, 0), 2) AS pct
        FROM app_data a, total t
        ORDER BY a.seconds DESC
        LIMIT :limit
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, "limit": limit, **sparams})

    _items = [
        AppDetailItem(
            app_name=r.app_name,
            category_id=r.category_id,
            category_name=r.category_name,
            color=r.color,
            seconds=int(r.seconds or 0),
            hours=round((r.seconds or 0) / 3600, 1),
            pct=float(r.pct or 0),
            user_count=int(r.user_count or 0),
        )
        for r in result.fetchall()
    ]
    await cache_set(_ckey, [i.model_dump() for i in _items], fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _items


@router.get("/utilization/adoption", response_model=AdoptionOut)
async def app_adoption(
    days:  int = Query(default=30, ge=1, le=90),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    top_n: int = Query(default=5, ge=1, le=20),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    clause, sparams = await _scope_clause(admin, db)

    top_apps_res = await db.execute(text(f"""
        SELECT normalize_app_name(ae.app_name) AS app_name, SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          {clause}
        GROUP BY normalize_app_name(ae.app_name)
        ORDER BY seconds DESC
        LIMIT :top_n
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, "top_n": top_n, **sparams})
    top_apps = [r.app_name for r in top_apps_res.fetchall()]
    if not top_apps:
        return AdoptionOut(apps=[], series=[])

    series_res = await db.execute(text(f"""
        SELECT
            DATE(ae.timestamp)  AS day,
            ae.app_name,
            COUNT(DISTINCT d.employee_id) AS user_count
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name = ANY(:apps)
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          {clause}
        GROUP BY day, ae.app_name
        ORDER BY day ASC
    """), {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt, "apps": top_apps, **sparams})

    by_date: dict[str, dict[str, int]] = {}
    for r in series_res.fetchall():
        d = str(r.day)
        if d not in by_date:
            by_date[d] = {a: 0 for a in top_apps}
        by_date[d][r.app_name] = r.user_count

    series = [{"date": d, **counts} for d, counts in sorted(by_date.items())]
    return AdoptionOut(apps=top_apps, series=series)


# ── Uncategorized apps + URL hosts from devices ───────────────────────────────

from app.schemas.tech import UncategorizedItem, UncategorizedOut  # noqa: E402


@router.get("/uncategorized", response_model=UncategorizedOut)
async def get_uncategorized(
    days:  int = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    limit: int = Query(default=200, ge=1, le=500),
    admin: Employee = Depends(require("categories", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Return apps and URL hosts seen on devices that have no category rule yet."""
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    await _ensure_default_categories(admin.company_id, db)
    clause, sparams = await _scope_clause(admin, db)
    cid = str(admin.company_id)  # hoisted before cache helpers (rollback safety)
    params = {"company_id": cid, "since_dt": since_dt, "until_dt": until_dt, "limit": limit, **sparams}

    _scope_fp = "all" if not clause else str(admin.id)
    _ckey = ("tech:uncategorized", cid, _scope_fp, str(since_dt), str(until_dt), limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admins read from the daily aggregate caches — the
    # live version below is a 30-day two-scan aggregation measured at ~67 s on
    # a 42M-row events table.
    if not clause:
        from app.core import tech_cache
        cached = await tech_cache.uncategorized_from_cache(db, cid, since_dt, until_dt, limit)
        if cached is not None:
            capps, curls = cached
            _result = UncategorizedOut(
                apps=[
                    UncategorizedItem(pattern=r.pattern, kind="app",
                                      seconds=int(r.seconds or 0),
                                      user_count=int(r.user_count or 0),
                                      last_seen=str(r.last_seen) if r.last_seen else None)
                    for r in capps if r.pattern
                ],
                urls=[
                    UncategorizedItem(pattern=r.pattern, kind="url",
                                      seconds=int(r.seconds or 0),
                                      user_count=int(r.user_count or 0),
                                      last_seen=str(r.last_seen) if r.last_seen else None)
                    for r in curls if r.pattern
                ],
            )
            await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _result

    apps_res = await db.execute(text(f"""
        SELECT
            normalize_app_name(ae.app_name)       AS pattern,
            'app'                                 AS kind,
            SUM(ae.duration)                      AS seconds,
            COUNT(DISTINCT d.employee_id)         AS user_count,
            MAX(ae.timestamp)                     AS last_seen
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          AND NOT EXISTS (
              SELECT 1 FROM app_category_rules r
              WHERE r.company_id = d.company_id
                AND r.app_pattern = normalize_app_name(ae.app_name)
          )
          {clause}
        GROUP BY normalize_app_name(ae.app_name)
        ORDER BY seconds DESC
        LIMIT :limit
    """), params)

    urls_res = await db.execute(text(f"""
        SELECT
            substring(ae.url FROM '^https?://([^/?#]+)') AS pattern,
            'url'                                         AS kind,
            COUNT(*)                                      AS seconds,
            COUNT(DISTINCT d.employee_id)                AS user_count,
            MAX(ae.timestamp)                            AS last_seen
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'url_visit'
          AND ae.url IS NOT NULL
          AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
          AND NOT EXISTS (
              SELECT 1 FROM app_category_rules r
              WHERE r.company_id = d.company_id
                AND r.app_pattern = substring(ae.url FROM '^https?://([^/?#]+)')
          )
          {clause}
        GROUP BY substring(ae.url FROM '^https?://([^/?#]+)')
        HAVING substring(ae.url FROM '^https?://([^/?#]+)') IS NOT NULL
        ORDER BY seconds DESC
        LIMIT :limit
    """), params)

    apps = [
        UncategorizedItem(
            pattern=r.pattern,
            kind="app",
            seconds=int(r.seconds or 0),
            user_count=int(r.user_count or 0),
            last_seen=str(r.last_seen) if r.last_seen else None,
        )
        for r in apps_res.fetchall()
        if r.pattern
    ]
    urls = [
        UncategorizedItem(
            pattern=r.pattern,
            kind="url",
            seconds=int(r.seconds or 0),
            user_count=int(r.user_count or 0),
            last_seen=str(r.last_seen) if r.last_seen else None,
        )
        for r in urls_res.fetchall()
        if r.pattern
    ]
    _result = UncategorizedOut(apps=apps, urls=urls)
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


# ── Shared helpers ────────────────────────────────────────────────────────────

PRODUCTIVE_CATS   = {"productivity", "development", "communication", "meetings"}
UNPRODUCTIVE_CATS = {"media"}

async def _productivity_map(company_id: uuid.UUID, db: AsyncSession) -> dict[str, tuple[str, str, str]]:
    """Returns {app_pattern: (productivity, category_name, color)}."""
    res = await db.execute(text("""
        SELECT r.app_pattern, c.name, c.color
        FROM app_category_rules r
        JOIN app_categories c ON c.id = r.category_id
        WHERE r.company_id = :cid
    """), {"cid": str(company_id)})
    out: dict[str, tuple[str, str, str]] = {}
    for row in res.fetchall():
        cat_lower = row.name.lower()
        if cat_lower in PRODUCTIVE_CATS:
            prod = "productive"
        elif cat_lower in UNPRODUCTIVE_CATS:
            prod = "unproductive"
        else:
            prod = "neutral"
        out[row.app_pattern] = (prod, row.name, row.color)
    return out


def _employee_clause(employee_id: Optional[str]) -> tuple[str, dict]:
    """Restrict to a single employee's devices."""
    if not employee_id:
        return "", {}
    return " AND d.employee_id = :emp_id", {"emp_id": employee_id}


async def _group_clause(group_id: Optional[str], company_id, db: AsyncSession) -> tuple[str, dict]:
    """Restrict to a group's devices. The group MUST belong to the caller's
    company — a foreign group id resolves to an empty result, never to another
    tenant's devices."""
    if not group_id:
        return "", {}
    try:
        gid = uuid.UUID(group_id)
    except ValueError:
        return " AND 1=0", {}
    row = await db.execute(
        select(Group.id).where(
            Group.id == gid,
            Group.company_id == company_id,
            Group.is_deleted == False,  # noqa: E712
        )
    )
    if not row.scalar_one_or_none():
        return " AND 1=0", {}
    expanded = await descendant_group_ids([gid], company_id, db)
    ids = [str(d) for d in await devices_for_groups(expanded, db)]
    if not ids:
        return " AND 1=0", {}
    return " AND ae.device_id = ANY(:gids)", {"gids": ids}


# ── Website Usage ─────────────────────────────────────────────────────────────

@router.get("/websites", response_model=WebsiteListOut)
async def website_list(
    days:        int           = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    productivity: Optional[str] = Query(default=None),
    group_id:    Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    limit:       int           = Query(default=100, ge=1, le=500),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    await _ensure_default_categories(admin.company_id, db)
    scope_clause, scope_params = await _scope_clause(admin, db)
    grp_clause,   grp_params   = await _group_clause(group_id, admin.company_id, db)
    emp_clause,   emp_params   = _employee_clause(employee_id)
    combined_clause = scope_clause + grp_clause + emp_clause
    cid = str(admin.company_id)  # hoisted before cache helpers (rollback safety)
    params = {"company_id": cid, "since_dt": since_dt, "until_dt": until_dt, "limit": limit,
              **scope_params, **grp_params, **emp_params}

    _scope_fp = "all" if not combined_clause else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:websites", cid, _scope_fp, productivity or "", str(since_dt), str(until_dt), limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admin with no filters reads per-host daily
    # aggregates instead of window-functioning every url_visit row in range.
    rows = None
    if not combined_clause:
        from app.core import tech_cache
        rows = await tech_cache.website_list_from_cache(db, cid, since_dt, until_dt, limit)

    if rows is None:
        res = await db.execute(text(f"""
        WITH url_data AS (
            SELECT
                REGEXP_REPLACE(LOWER(
                    COALESCE(substring(ae.url FROM '^https?://(?\\:www\\.)?([^/?#]+)'), '')
                ), '^www\\.', '') AS host,
                ae.device_id, ae.timestamp, d.employee_id,
                LEAD(ae.timestamp) OVER (PARTITION BY ae.device_id ORDER BY ae.timestamp) AS next_ts
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit'
              AND ae.url IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined_clause}
        ),
        dur AS (
            SELECT host, employee_id,
                GREATEST(LEAST(
                    EXTRACT(EPOCH FROM (
                        COALESCE(next_ts, timestamp + interval '3 minutes') - timestamp
                    ))::int, 600
                ), 30) AS secs
            FROM url_data WHERE host IS NOT NULL AND host <> ''
        ),
        agg AS (
            SELECT host, SUM(secs) AS total_secs,
                   COUNT(*) AS visits, COUNT(DISTINCT employee_id) AS users
            FROM dur GROUP BY host
        ),
        tot AS (SELECT NULLIF(SUM(total_secs), 0) AS t FROM agg)
        SELECT a.host, a.total_secs, a.visits, a.users,
               ROUND(100.0 * a.total_secs / t.t, 1) AS pct
        FROM agg a, tot t
        ORDER BY a.total_secs DESC
        LIMIT :limit
        """), params)
        rows = res.fetchall()

    prod_map = await _productivity_map(admin.company_id, db)

    items: list[WebsiteItem] = []
    for r in rows:
        host = r.host
        # Match host against category rules (exact or suffix)
        match = prod_map.get(host)
        if not match:
            for pat, val in prod_map.items():
                if host.endswith(pat) or pat.endswith(host):
                    match = val
                    break
        prod, cat_name, color = match if match else ("neutral", None, "#94a3b8")
        if productivity and prod != productivity:
            continue
        items.append(WebsiteItem(
            host=host, duration_secs=int(r.total_secs or 0),
            visits=int(r.visits or 0), user_count=int(r.users or 0),
            pct=float(r.pct or 0), productivity=prod,
            category_name=cat_name, color=color,
        ))

    total_secs  = sum(i.duration_secs for i in items)
    total_visits = sum(i.visits for i in items)
    user_set = set()
    _result = WebsiteListOut(items=items, total_secs=total_secs,
                             total_visits=total_visits, total_users=len(user_set))
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


@router.get("/websites/detail", response_model=WebsiteDetailOut)
async def website_detail(
    host:        str            = Query(...),
    tab:         str            = Query(default="titles"),
    days:        int            = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    group_id:    Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    scope_clause, scope_params = await _scope_clause(admin, db)
    grp_clause,   grp_params   = await _group_clause(group_id, admin.company_id, db)
    emp_clause,   emp_params   = _employee_clause(employee_id)
    combined = scope_clause + grp_clause + emp_clause
    params = {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt,
              "host": host, **scope_params, **grp_params, **emp_params}

    _scope_fp = "all" if not combined else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:web_detail", str(admin.company_id), _scope_fp, host, tab, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    out = WebsiteDetailOut(host=host)

    if tab == "titles":
        res = await db.execute(text(f"""
            SELECT
                COALESCE(NULLIF(substring(ae.url FROM '^https?://[^/?#]+([^?#]*)'), ''), '/') AS path,
                COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit'
              AND LOWER(REGEXP_REPLACE(
                    COALESCE(substring(ae.url FROM '^https?://(?\\:www\\.)?([^/?#]+)'), ''),
                    '^www\\.', '')) = LOWER(:host)
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY path ORDER BY visits DESC LIMIT 50
        """), params)
        rows = res.fetchall()
        total = sum(r.visits for r in rows) or 1
        out.titles = [
            WebsiteDetailTitle(title=r.path, visits=int(r.visits),
                               pct=round(100.0 * r.visits / total, 1))
            for r in rows
        ]

    elif tab == "subpages":
        res = await db.execute(text(f"""
            SELECT ae.url AS full_url, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit'
              AND LOWER(REGEXP_REPLACE(
                    COALESCE(substring(ae.url FROM '^https?://(?\\:www\\.)?([^/?#]+)'), ''),
                    '^www\\.', '')) = LOWER(:host)
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY ae.url ORDER BY visits DESC LIMIT 100
        """), params)
        rows = res.fetchall()
        total = sum(r.visits for r in rows) or 1
        out.subpages = [
            WebsiteDetailSubpage(path=r.full_url, visits=int(r.visits),
                                 pct=round(100.0 * r.visits / total, 1))
            for r in rows
        ]

    elif tab == "users":
        res = await db.execute(text(f"""
            WITH url_data AS (
                SELECT d.employee_id,
                    LEAD(ae.timestamp) OVER (PARTITION BY ae.device_id ORDER BY ae.timestamp) AS next_ts,
                    ae.timestamp
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                WHERE d.company_id = :company_id
                  AND ae.event_type = 'url_visit'
                  AND LOWER(REGEXP_REPLACE(
                        COALESCE(substring(ae.url FROM '^https?://(?\\:www\\.)?([^/?#]+)'), ''),
                        '^www\\.', '')) = LOWER(:host)
                  AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
                  {combined}
            ),
            dur AS (
                SELECT employee_id,
                    GREATEST(LEAST(EXTRACT(EPOCH FROM (
                        COALESCE(next_ts, timestamp + interval '3 minutes') - timestamp
                    ))::int, 600), 30) AS secs
                FROM url_data
            )
            SELECT e.name, e.email, SUM(d.secs) AS total_secs
            FROM dur d
            JOIN employees e ON e.id = d.employee_id
            GROUP BY e.name, e.email
            ORDER BY total_secs DESC LIMIT 50
        """), params)
        rows = res.fetchall()
        total = sum(r.total_secs for r in rows) or 1
        out.users = [
            WebsiteDetailUser(name=r.name, visits=0,
                              duration_secs=int(r.total_secs or 0),
                              pct=round(100.0 * (r.total_secs or 0) / total, 1))
            for r in rows
        ]

    elif tab == "usage":
        res = await db.execute(text(f"""
            SELECT DATE(ae.timestamp) AS day, COUNT(*) AS visits
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'url_visit'
              AND LOWER(REGEXP_REPLACE(
                    COALESCE(substring(ae.url FROM '^https?://(?\\:www\\.)?([^/?#]+)'), ''),
                    '^www\\.', '')) = LOWER(:host)
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY day ORDER BY day ASC
        """), params)
        out.usage = [
            WebsiteDetailUsagePoint(date=str(r.day), visits=int(r.visits))
            for r in res.fetchall()
        ]

    await cache_set(_ckey, out.model_dump(), fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
    return out


# ── Application Usage ─────────────────────────────────────────────────────────

@router.get("/apps", response_model=AppUsageListOut)
async def app_usage_list(
    days:        int           = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    productivity: Optional[str] = Query(default=None),
    group_id:    Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    limit:       int           = Query(default=100, ge=1, le=500),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    await _ensure_default_categories(admin.company_id, db)
    scope_clause, scope_params = await _scope_clause(admin, db)
    grp_clause,   grp_params   = await _group_clause(group_id, admin.company_id, db)
    emp_clause,   emp_params   = _employee_clause(employee_id)
    combined = scope_clause + grp_clause + emp_clause
    cid = str(admin.company_id)  # hoisted before cache helpers (rollback safety)
    params = {"company_id": cid, "since_dt": since_dt, "until_dt": until_dt,
              "limit": limit, **scope_params, **grp_params, **emp_params}

    _scope_fp = "all" if not combined else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:apps", cid, _scope_fp, productivity or "", str(since_dt), str(until_dt), limit)
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admin with no filters reads the per-app daily cache.
    rows = None
    if not combined:
        from app.core import tech_cache
        rows = await tech_cache.app_usage_from_cache(db, cid, since_dt, until_dt, limit)

    if rows is None:
        res = await db.execute(text(f"""
            WITH agg AS (
                SELECT normalize_app_name(ae.app_name) AS app_name,
                    SUM(ae.duration) AS total_secs,
                    COUNT(DISTINCT d.employee_id) AS users
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                WHERE d.company_id = :company_id
                  AND ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL
                  AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
                  {combined}
                GROUP BY normalize_app_name(ae.app_name)
            ),
            tot AS (SELECT NULLIF(SUM(total_secs), 0) AS t FROM agg)
            SELECT a.app_name, a.total_secs, a.users,
                   ROUND(100.0 * a.total_secs / t.t, 1) AS pct
            FROM agg a, tot t
            ORDER BY a.total_secs DESC
            LIMIT :limit
        """), params)
        rows = res.fetchall()

    prod_map = await _productivity_map(admin.company_id, db)

    items: list[AppUsageItem] = []
    for r in rows:
        match = prod_map.get(r.app_name)
        prod, cat_name, color = match if match else ("neutral", None, "#94a3b8")
        if productivity and prod != productivity:
            continue
        items.append(AppUsageItem(
            app_name=r.app_name, duration_secs=int(r.total_secs or 0),
            user_count=int(r.users or 0), pct=float(r.pct or 0),
            productivity=prod, category_name=cat_name, color=color,
        ))

    _result = AppUsageListOut(
        items=items,
        total_secs=sum(i.duration_secs for i in items),
        total_apps=len(items),
        total_users=0,
    )
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


@router.get("/apps/detail", response_model=AppUsageDetailOut)
async def app_usage_detail(
    app_name:    str            = Query(...),
    tab:         str            = Query(default="titles"),
    days:        int            = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    group_id:    Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    scope_clause, scope_params = await _scope_clause(admin, db)
    grp_clause,   grp_params   = await _group_clause(group_id, admin.company_id, db)
    emp_clause,   emp_params   = _employee_clause(employee_id)
    combined = scope_clause + grp_clause + emp_clause
    params = {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt,
              "app_name": app_name, **scope_params, **grp_params, **emp_params}

    _scope_fp = "all" if not combined else f"{admin.id}:{group_id or ''}:{employee_id or ''}"
    _ckey = ("tech:app_detail", str(admin.company_id), _scope_fp, app_name, tab, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    out = AppUsageDetailOut(app_name=app_name)

    if tab == "titles":
        res = await db.execute(text(f"""
            SELECT ae.window_title AS title, SUM(ae.duration) AS total_secs
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND normalize_app_name(ae.app_name) = :app_name
              AND ae.window_title IS NOT NULL AND ae.window_title <> ''
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY ae.window_title
            ORDER BY total_secs DESC LIMIT 50
        """), params)
        rows = res.fetchall()
        total = sum(r.total_secs for r in rows) or 1
        out.titles = [
            AppUsageDetailTitle(title=r.title, duration_secs=int(r.total_secs or 0),
                                pct=round(100.0 * (r.total_secs or 0) / total, 1))
            for r in rows
        ]

    elif tab == "users":
        res = await db.execute(text(f"""
            SELECT e.name, e.email, SUM(ae.duration) AS total_secs
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN employees e ON e.id = d.employee_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND normalize_app_name(ae.app_name) = :app_name
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY e.name, e.email
            ORDER BY total_secs DESC LIMIT 50
        """), params)
        rows = res.fetchall()
        total = sum(r.total_secs for r in rows) or 1
        out.users = [
            AppUsageDetailUser(name=r.name, duration_secs=int(r.total_secs or 0),
                               pct=round(100.0 * (r.total_secs or 0) / total, 1))
            for r in rows
        ]

    elif tab == "usage":
        res = await db.execute(text(f"""
            SELECT DATE(ae.timestamp) AS day, SUM(ae.duration) AS total_secs
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND normalize_app_name(ae.app_name) = :app_name
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY day ORDER BY day ASC
        """), params)
        out.usage = [
            AppUsageDetailPoint(date=str(r.day), duration_secs=int(r.total_secs or 0))
            for r in res.fetchall()
        ]

    await cache_set(_ckey, out.model_dump(), fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
    return out


# ── Category Usage ────────────────────────────────────────────────────────────

@router.get("/category-usage", response_model=CategoryUsageListOut)
async def category_usage_list(
    days:     int           = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    group_id: Optional[str] = Query(default=None),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    await _ensure_default_categories(admin.company_id, db)
    scope_clause, scope_params = await _scope_clause(admin, db)
    grp_clause,   grp_params   = await _group_clause(group_id, admin.company_id, db)
    combined = scope_clause + grp_clause
    cid = str(admin.company_id)  # hoisted before cache helpers (rollback safety)
    params = {"company_id": cid, "since_dt": since_dt, "until_dt": until_dt,
              **scope_params, **grp_params}

    _scope_fp = "all" if not combined else f"{admin.id}:{group_id or ''}"
    _ckey = ("tech:cat_usage", cid, _scope_fp, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Fast path: full-scope admin with no group filter reads the per-category
    # daily cache instead of two aggregation scans over the range.
    if not combined:
        from app.core import tech_cache
        cached_rows = await tech_cache.category_usage_from_cache(db, cid, since_dt, until_dt)
        if cached_rows is not None:
            items = [
                CategoryUsageItem(
                    cat_id=r.cat_id, name=r.name, color=r.color,
                    duration_secs=int(r.total_secs or 0),
                    user_count=int(r.users or 0),
                    pct=float(r.pct or 0),
                )
                for r in cached_rows
            ]
            _result = CategoryUsageListOut(
                items=items,
                total_secs=sum(i.duration_secs for i in items),
                total_users=max((i.user_count for i in items), default=0),
            )
            await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
            return _result

    res = await db.execute(text(f"""
        WITH categorized AS (
            SELECT c.id::text AS cat_id, c.name, c.color,
                SUM(ae.duration) AS total_secs,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name)
                AND r.company_id = d.company_id
            JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
            GROUP BY c.id, c.name, c.color
        ),
        uncategorized AS (
            SELECT NULL::text AS cat_id, 'Uncategorized' AS name,
                   '#94a3b8' AS color,
                SUM(ae.duration) AS total_secs,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id = d.company_id AND r2.app_pattern = normalize_app_name(ae.app_name)
              )
              {combined}
            GROUP BY name, color
            HAVING SUM(ae.duration) > 0
        ),
        combined AS (SELECT * FROM categorized UNION ALL SELECT * FROM uncategorized),
        tot AS (SELECT NULLIF(SUM(total_secs), 0) AS t FROM combined)
        SELECT c.cat_id, c.name, c.color, c.total_secs, c.users,
               ROUND(100.0 * c.total_secs / t.t, 1) AS pct
        FROM combined c, tot t
        ORDER BY c.total_secs DESC
    """), params)

    items = [
        CategoryUsageItem(
            cat_id=r.cat_id, name=r.name, color=r.color,
            duration_secs=int(r.total_secs or 0),
            user_count=int(r.users or 0),
            pct=float(r.pct or 0),
        )
        for r in res.fetchall()
    ]
    _result = CategoryUsageListOut(
        items=items,
        total_secs=sum(i.duration_secs for i in items),
        total_users=max((i.user_count for i in items), default=0),
    )
    await cache_set(_ckey, _result.model_dump(), fresh_secs=FRESH_ANALYTICS, stale_secs=STALE_ANALYTICS)
    return _result


@router.get("/category-usage/detail", response_model=CategoryUsageDetailOut)
async def category_usage_detail(
    cat_id:   Optional[str] = Query(default=None),
    tab:      str            = Query(default="activities"),
    days:     int            = Query(default=30, ge=1, le=365),
    since_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    until_date:  Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    group_id: Optional[str] = Query(default=None),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    since_dt, until_dt = _resolve_dates(days, since_date, until_date)
    scope_clause, scope_params = await _scope_clause(admin, db)
    grp_clause,   grp_params   = await _group_clause(group_id, admin.company_id, db)
    combined = scope_clause + grp_clause
    params = {"company_id": str(admin.company_id), "since_dt": since_dt, "until_dt": until_dt,
              **scope_params, **grp_params}

    _scope_fp = "all" if not combined else f"{admin.id}:{group_id or ''}"
    _ckey = ("tech:cat_detail", str(admin.company_id), _scope_fp, cat_id or "", tab, str(since_dt), str(until_dt))
    _cached, _ = await cache_get(_ckey)
    if _cached is not None:
        return _cached

    # Resolve category name + color
    if cat_id:
        cat_res = await db.execute(text(
            "SELECT name, color FROM app_categories WHERE id = :cid AND company_id = :company_id"
        ), {"cid": cat_id, "company_id": str(admin.company_id)})
        cat_row = cat_res.fetchone()
        if not cat_row:
            raise HTTPException(status_code=404, detail="Category not found")
        cat_name, cat_color = cat_row.name, cat_row.color
    else:
        cat_name, cat_color = "Uncategorized", "#94a3b8"

    out = CategoryUsageDetailOut(cat_id=cat_id, name=cat_name, color=cat_color, total_secs=0)

    # Total secs for this category
    if cat_id:
        tot_res = await db.execute(text(f"""
            SELECT COALESCE(SUM(ae.duration), 0) AS total_secs
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name)
                AND r.company_id = d.company_id
            WHERE d.company_id = :company_id
              AND r.category_id = :cat_id
              AND ae.event_type = 'window_change'
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              {combined}
        """), {**params, "cat_id": cat_id})
    else:
        tot_res = await db.execute(text(f"""
            SELECT COALESCE(SUM(ae.duration), 0) AS total_secs
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id = d.company_id AND r2.app_pattern = normalize_app_name(ae.app_name)
              )
              {combined}
        """), params)
    out.total_secs = int(tot_res.fetchone().total_secs or 0)

    if tab == "activities":
        if cat_id:
            res = await db.execute(text(f"""
                SELECT normalize_app_name(ae.app_name) AS app_name, SUM(ae.duration) AS total_secs
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name)
                    AND r.company_id = d.company_id
                WHERE d.company_id = :company_id
                  AND r.category_id = :cat_id
                  AND ae.event_type = 'window_change'
                  AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
                  {combined}
                GROUP BY normalize_app_name(ae.app_name) ORDER BY total_secs DESC LIMIT 50
            """), {**params, "cat_id": cat_id})
        else:
            res = await db.execute(text(f"""
                SELECT normalize_app_name(ae.app_name) AS app_name, SUM(ae.duration) AS total_secs
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                WHERE d.company_id = :company_id
                  AND ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL
                  AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
                  AND NOT EXISTS (
                      SELECT 1 FROM app_category_rules r2
                      WHERE r2.company_id = d.company_id AND r2.app_pattern = normalize_app_name(ae.app_name)
                  )
                  {combined}
                GROUP BY normalize_app_name(ae.app_name) ORDER BY total_secs DESC LIMIT 50
            """), params)
        rows = res.fetchall()
        total = out.total_secs or 1
        out.activities = [
            CategoryDetailActivity(app_name=r.app_name, duration_secs=int(r.total_secs or 0),
                                   pct=round(100.0 * (r.total_secs or 0) / total, 1))
            for r in rows
        ]

    elif tab == "users":
        if cat_id:
            res = await db.execute(text(f"""
                SELECT e.name, e.email, SUM(ae.duration) AS total_secs
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                JOIN employees e ON e.id = d.employee_id
                JOIN app_category_rules r ON r.app_pattern = normalize_app_name(ae.app_name)
                    AND r.company_id = d.company_id
                WHERE d.company_id = :company_id
                  AND r.category_id = :cat_id
                  AND ae.event_type = 'window_change'
                  AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
                  {combined}
                GROUP BY e.name, e.email ORDER BY total_secs DESC LIMIT 100
            """), {**params, "cat_id": cat_id})
        else:
            res = await db.execute(text(f"""
                SELECT e.name, e.email, SUM(ae.duration) AS total_secs
                FROM activity_events ae
                JOIN devices d ON d.id = ae.device_id
                JOIN employees e ON e.id = d.employee_id
                WHERE d.company_id = :company_id
                  AND ae.event_type = 'window_change'
                  AND ae.app_name IS NOT NULL
                  AND ae.timestamp >= CAST(:since_dt AS date) AND ae.timestamp < (CAST(:until_dt AS date) + INTERVAL '1 day')
                  AND NOT EXISTS (
                      SELECT 1 FROM app_category_rules r2
                      WHERE r2.company_id = d.company_id AND r2.app_pattern = normalize_app_name(ae.app_name)
                  )
                  {combined}
                GROUP BY e.name, e.email ORDER BY total_secs DESC LIMIT 100
            """), params)
        rows = res.fetchall()
        total = out.total_secs or 1
        out.users = [
            CategoryDetailUser(name=r.name, email=r.email, duration_secs=int(r.total_secs or 0),
                               pct=round(100.0 * (r.total_secs or 0) / total, 1))
            for r in rows
        ]

    await cache_set(_ckey, out.model_dump(), fresh_secs=FRESH_DETAIL, stale_secs=STALE_DETAIL)
    return out
