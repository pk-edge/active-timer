import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.api.deps import get_db, require
from app.core.scoping import visible_device_ids, ALL
from app.db.models import AppCategory, AppCategoryRule, Employee
from app.schemas.tech import (
    CategoryOut, CategoryRuleOut, CategoryCreate, CategoryUpdate, RuleCreate,
    CategoryAllocationItem, AppTrendsOut, AppTrendItem, TechSummaryOut,
    AppDetailItem, AdoptionOut,
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


async def _ensure_default_categories(company_id: uuid.UUID, db: AsyncSession) -> None:
    existing = await db.execute(
        select(AppCategory).where(AppCategory.company_id == company_id, AppCategory.is_system == True)
    )
    if existing.scalars().first():
        return

    for cat_def in DEFAULT_CATEGORIES:
        cat = AppCategory(
            company_id=company_id,
            name=cat_def["name"],
            color=cat_def["color"],
            is_system=True,
        )
        db.add(cat)
        await db.flush()

        patterns = DEFAULT_RULES.get(cat_def["name"], [])
        for pattern in patterns:
            existing_rule = await db.execute(
                select(AppCategoryRule).where(
                    AppCategoryRule.company_id == company_id,
                    AppCategoryRule.app_pattern == pattern,
                )
            )
            if not existing_rule.scalars().first():
                db.add(AppCategoryRule(
                    category_id=cat.id,
                    company_id=company_id,
                    app_pattern=pattern,
                ))

    await db.commit()


async def _scope_clause(admin: Employee, db: AsyncSession) -> tuple[str, dict]:
    ids = await visible_device_ids(admin, db)
    if ids is ALL:
        return "", {}
    return " AND ae.device_id = ANY(:vids)", {"vids": [str(i) for i in ids]}


# ── Category CRUD ─────────────────────────────────────────────────────────────

@router.get("/categories", response_model=List[CategoryOut])
async def list_categories(
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_default_categories(admin.company_id, db)
    result = await db.execute(
        select(AppCategory)
        .where(AppCategory.company_id == admin.company_id)
        .order_by(AppCategory.is_system.desc(), AppCategory.name)
    )
    cats = result.scalars().all()
    out = []
    for c in cats:
        rules_res = await db.execute(
            select(AppCategoryRule).where(AppCategoryRule.category_id == c.id)
        )
        rules = rules_res.scalars().all()
        out.append(CategoryOut(
            id=c.id,
            name=c.name,
            color=c.color,
            description=c.description,
            is_system=c.is_system,
            rule_count=len(rules),
            rules=[CategoryRuleOut(id=r.id, app_pattern=r.app_pattern, created_at=r.created_at) for r in rules],
        ))
    return out


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(
    body: CategoryCreate,
    admin: Employee = Depends(require("tech", "edit")),
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
    await db.commit()
    await db.refresh(cat)
    return CategoryOut(id=cat.id, name=cat.name, color=cat.color,
                       description=cat.description, is_system=cat.is_system,
                       rule_count=0, rules=[])


@router.put("/categories/{cat_id}", response_model=CategoryOut)
async def update_category(
    cat_id: str,
    body: CategoryUpdate,
    admin: Employee = Depends(require("tech", "edit")),
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
    admin: Employee = Depends(require("tech", "edit")),
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
    await db.delete(cat)
    await db.commit()


# ── Category Rules CRUD ───────────────────────────────────────────────────────

@router.post("/categories/{cat_id}/rules", response_model=CategoryRuleOut, status_code=201)
async def add_rule(
    cat_id: str,
    body: RuleCreate,
    admin: Employee = Depends(require("tech", "edit")),
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

    existing = await db.execute(
        select(AppCategoryRule).where(
            AppCategoryRule.company_id == admin.company_id,
            AppCategoryRule.app_pattern == body.app_pattern,
        )
    )
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail="App pattern already assigned to a category")

    rule = AppCategoryRule(category_id=cid, company_id=admin.company_id, app_pattern=body.app_pattern)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return CategoryRuleOut(id=rule.id, app_pattern=rule.app_pattern, created_at=rule.created_at)


@router.delete("/categories/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    admin: Employee = Depends(require("tech", "edit")),
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
    days: int = Query(default=30, ge=1, le=365),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_default_categories(admin.company_id, db)
    clause, sparams = await _scope_clause(admin, db)

    total_res = await db.execute(text(f"""
        SELECT
            COALESCE(SUM(ae.duration), 0)       AS total_seconds,
            COUNT(DISTINCT ae.app_name)          AS total_apps,
            COUNT(DISTINCT d.employee_id)        AS total_users
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          {clause}
    """), {"company_id": str(admin.company_id), "days": days, **sparams})
    row = total_res.fetchone()

    cat_res = await db.execute(text(f"""
        SELECT c.name, c.color, SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        JOIN app_category_rules r ON r.app_pattern = ae.app_name AND r.company_id = d.company_id
        JOIN app_categories c ON c.id = r.category_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          {clause}
        GROUP BY c.name, c.color
        ORDER BY seconds DESC
        LIMIT 1
    """), {"company_id": str(admin.company_id), "days": days, **sparams})
    top_cat = cat_res.fetchone()

    app_res = await db.execute(text(f"""
        SELECT ae.app_name, SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          {clause}
        GROUP BY ae.app_name
        ORDER BY seconds DESC
        LIMIT 1
    """), {"company_id": str(admin.company_id), "days": days, **sparams})
    top_app = app_res.fetchone()

    return TechSummaryOut(
        total_hours=round((row.total_seconds or 0) / 3600, 1),
        total_apps=int(row.total_apps or 0),
        total_users=int(row.total_users or 0),
        top_category=top_cat.name if top_cat else None,
        top_category_color=top_cat.color if top_cat else "#6366f1",
        top_app=top_app.app_name if top_app else None,
    )


@router.get("/utilization/category-allocation", response_model=List[CategoryAllocationItem])
async def category_allocation(
    days:         int = Query(default=30, ge=1, le=365),
    compare_days: int = Query(default=30, ge=1, le=365),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_default_categories(admin.company_id, db)
    clause, sparams = await _scope_clause(admin, db)

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
            JOIN app_category_rules r ON r.app_pattern = ae.app_name AND r.company_id = d.company_id
            JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
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
              AND ae.timestamp >= NOW() - make_interval(days => :days)
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id = d.company_id AND r2.app_pattern = ae.app_name
              )
              {clause}
            GROUP BY category_name, color
        ),
        combined AS (SELECT * FROM categorized UNION ALL SELECT * FROM uncategorized),
        total AS (SELECT SUM(seconds) AS t FROM combined)
        SELECT c.*, ROUND(100.0 * c.seconds / NULLIF(t.t, 0), 1) AS pct
        FROM combined c, total t
        ORDER BY seconds DESC
    """), {"company_id": str(admin.company_id), "days": days, **sparams})
    current_rows = {r.category_name: r for r in current_res.fetchall()}

    prev_res = await db.execute(text(f"""
        WITH categorized AS (
            SELECT
                c.name AS category_name,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            JOIN app_category_rules r ON r.app_pattern = ae.app_name AND r.company_id = d.company_id
            JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days) - make_interval(days => :cdays)
              AND ae.timestamp <  NOW() - make_interval(days => :days)
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
              AND ae.timestamp >= NOW() - make_interval(days => :days) - make_interval(days => :cdays)
              AND ae.timestamp <  NOW() - make_interval(days => :days)
              AND NOT EXISTS (
                  SELECT 1 FROM app_category_rules r2
                  WHERE r2.company_id = d.company_id AND r2.app_pattern = ae.app_name
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
    """), {"company_id": str(admin.company_id), "days": days, "cdays": compare_days, **sparams})
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
    return result


@router.get("/utilization/app-trends", response_model=AppTrendsOut)
async def app_trends(
    days:         int = Query(default=30, ge=1, le=365),
    compare_days: int = Query(default=30, ge=1, le=365),
    limit:        int = Query(default=10, ge=1, le=50),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_default_categories(admin.company_id, db)
    clause, sparams = await _scope_clause(admin, db)

    base_params = {"company_id": str(admin.company_id), "days": days, "cdays": compare_days, **sparams}

    current_res = await db.execute(text(f"""
        WITH app_data AS (
            SELECT
                ae.app_name,
                COALESCE(c.id::text, NULL) AS category_id,
                COALESCE(c.name, 'Uncategorized') AS category_name,
                COALESCE(c.color, '#94a3b8') AS color,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            LEFT JOIN app_category_rules r ON r.app_pattern = ae.app_name AND r.company_id = d.company_id
            LEFT JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
              {clause}
            GROUP BY ae.app_name, c.id, c.name, c.color
        ),
        total AS (SELECT SUM(seconds) AS t FROM app_data)
        SELECT a.*, ROUND(100.0 * a.seconds / NULLIF(t.t, 0), 2) AS pct
        FROM app_data a, total t
    """), {"company_id": str(admin.company_id), "days": days, **sparams})
    cur_rows = {r.app_name: r for r in current_res.fetchall()}

    prev_res = await db.execute(text(f"""
        WITH app_data AS (
            SELECT
                ae.app_name,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS users
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days) - make_interval(days => :cdays)
              AND ae.timestamp <  NOW() - make_interval(days => :days)
              {clause}
            GROUP BY ae.app_name
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

    return AppTrendsOut(trending_up=trending_up, trending_down=trending_down)


@router.get("/utilization/apps", response_model=List[AppDetailItem])
async def app_list(
    days:        int           = Query(default=30, ge=1, le=365),
    category_id: Optional[str] = Query(default=None),
    limit:       int           = Query(default=50, ge=1, le=200),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_default_categories(admin.company_id, db)
    clause, sparams = await _scope_clause(admin, db)

    cat_filter = ""
    if category_id:
        try:
            uuid.UUID(category_id)
            cat_filter = " AND c.id = :cat_id"
            sparams["cat_id"] = category_id
        except ValueError:
            pass

    result = await db.execute(text(f"""
        WITH app_data AS (
            SELECT
                ae.app_name,
                COALESCE(c.id::text, NULL) AS category_id,
                COALESCE(c.name, 'Uncategorized') AS category_name,
                COALESCE(c.color, '#94a3b8') AS color,
                SUM(ae.duration) AS seconds,
                COUNT(DISTINCT d.employee_id) AS user_count
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            LEFT JOIN app_category_rules r ON r.app_pattern = ae.app_name AND r.company_id = d.company_id
            LEFT JOIN app_categories c ON c.id = r.category_id
            WHERE d.company_id = :company_id
              AND ae.event_type = 'window_change'
              AND ae.app_name IS NOT NULL
              AND ae.timestamp >= NOW() - make_interval(days => :days)
              {cat_filter}
              {clause}
            GROUP BY ae.app_name, c.id, c.name, c.color
        ),
        total AS (SELECT SUM(seconds) AS t FROM app_data)
        SELECT a.*, ROUND(100.0 * a.seconds / NULLIF(t.t, 0), 2) AS pct
        FROM app_data a, total t
        ORDER BY a.seconds DESC
        LIMIT :limit
    """), {"company_id": str(admin.company_id), "days": days, "limit": limit, **sparams})

    return [
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


@router.get("/utilization/adoption", response_model=AdoptionOut)
async def app_adoption(
    days:  int = Query(default=30, ge=1, le=90),
    top_n: int = Query(default=5, ge=1, le=20),
    admin: Employee = Depends(require("tech", "view")),
    db: AsyncSession = Depends(get_db),
):
    clause, sparams = await _scope_clause(admin, db)

    top_apps_res = await db.execute(text(f"""
        SELECT ae.app_name, SUM(ae.duration) AS seconds
        FROM activity_events ae
        JOIN devices d ON d.id = ae.device_id
        WHERE d.company_id = :company_id
          AND ae.event_type = 'window_change'
          AND ae.app_name IS NOT NULL
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          {clause}
        GROUP BY ae.app_name
        ORDER BY seconds DESC
        LIMIT :top_n
    """), {"company_id": str(admin.company_id), "days": days, "top_n": top_n, **sparams})
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
          AND ae.timestamp >= NOW() - make_interval(days => :days)
          {clause}
        GROUP BY day, ae.app_name
        ORDER BY day ASC
    """), {"company_id": str(admin.company_id), "days": days, "apps": top_apps, **sparams})

    by_date: dict[str, dict[str, int]] = {}
    for r in series_res.fetchall():
        d = str(r.day)
        if d not in by_date:
            by_date[d] = {a: 0 for a in top_apps}
        by_date[d][r.app_name] = r.user_count

    series = [{"date": d, **counts} for d, counts in sorted(by_date.items())]
    return AdoptionOut(apps=top_apps, series=series)
