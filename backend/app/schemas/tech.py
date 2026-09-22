import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


# ── Categories ────────────────────────────────────────────────────────────────

class CategoryRuleOut(BaseModel):
    id:          uuid.UUID
    app_pattern: str
    created_at:  datetime

    model_config = {"from_attributes": True}


class CategoryOut(BaseModel):
    id:          uuid.UUID
    name:        str
    color:       str
    description: Optional[str]
    is_system:   bool
    rule_count:  int = 0
    rules:       list[CategoryRuleOut] = []

    model_config = {"from_attributes": True}


class CategoryCreate(BaseModel):
    name:        str
    color:       str = "#6366f1"
    description: Optional[str] = None


class CategoryUpdate(BaseModel):
    name:        Optional[str] = None
    color:       Optional[str] = None
    description: Optional[str] = None


class RuleCreate(BaseModel):
    app_pattern: str


class CategoriesPageOut(BaseModel):
    items:       list[CategoryOut]
    total:       int
    page:        int
    per_page:    int
    total_pages: int


# ── Filter lookups ─────────────────────────────────────────────────────────────

class TechFilterUserOut(BaseModel):
    id:        str
    name:      str
    email:     str
    is_active: bool


class TechFilterDeviceOut(BaseModel):
    id:            str
    hostname:      str
    # Null for a device not yet assigned to anyone. The Tech pages' device
    # search still lists it — an unassigned machine is exactly the case the
    # talent filter cannot reach.
    employee_name: Optional[str] = None
    platform:      str
    is_active:     bool


# ── Utilization analytics ─────────────────────────────────────────────────────

class CategoryAllocationItem(BaseModel):
    category_id:   Optional[str]
    category_name: str
    color:         str
    seconds:       int
    pct:           float
    prev_seconds:  int
    prev_pct:      float
    pct_change:    float
    hours:         float
    user_change:   int


class AppTrendItem(BaseModel):
    app_name:      str
    category_name: Optional[str]
    color:         str
    seconds:       int
    prev_seconds:  int
    pct:           float
    prev_pct:      float
    pct_change:    float
    hours:         float
    user_change:   int


class AppTrendsOut(BaseModel):
    trending_up:   list[AppTrendItem]
    trending_down: list[AppTrendItem]


class TechSummaryOut(BaseModel):
    total_hours:         float
    total_apps:          int
    total_users:         int
    top_category:        Optional[str]
    top_category_color:  str
    top_app:             Optional[str]


class AppDetailItem(BaseModel):
    app_name:      str
    category_id:   Optional[str]
    category_name: str
    color:         str
    seconds:       int
    hours:         float
    pct:           float
    user_count:    int


class AdoptionPoint(BaseModel):
    date:      str
    app_name:  str
    user_count: int


class AdoptionOut(BaseModel):
    apps:   list[str]
    series: list[dict]


class UncategorizedItem(BaseModel):
    pattern:    str
    kind:       str          # "app" | "url"
    seconds:    int
    user_count: int
    last_seen:  Optional[str]


class UncategorizedOut(BaseModel):
    apps: list[UncategorizedItem]
    urls: list[UncategorizedItem]


# ── Website Usage ─────────────────────────────────────────────────────────────

class WebsiteItem(BaseModel):
    host:          str
    duration_secs: int
    visits:        int
    user_count:    int
    pct:           float
    productivity:  str          # "productive" | "unproductive" | "neutral"
    category_name: Optional[str]
    color:         str


class WebsiteDetailTitle(BaseModel):
    title:    str
    visits:   int
    pct:      float


class WebsiteDetailSubpage(BaseModel):
    path:     str
    visits:   int
    pct:      float


class WebsiteDetailUser(BaseModel):
    name:          str
    visits:        int
    duration_secs: int
    pct:           float


class WebsiteDetailUsagePoint(BaseModel):
    date:   str
    visits: int


class WebsiteDetailOut(BaseModel):
    host:      str
    titles:    list[WebsiteDetailTitle]    = []
    subpages:  list[WebsiteDetailSubpage]  = []
    users:     list[WebsiteDetailUser]     = []
    usage:     list[WebsiteDetailUsagePoint] = []


class WebsiteListOut(BaseModel):
    items:        list[WebsiteItem]
    total_secs:   int
    total_visits: int
    total_users:  int


# ── Application Usage ─────────────────────────────────────────────────────────

class AppUsageItem(BaseModel):
    app_name:      str
    duration_secs: int
    user_count:    int
    pct:           float
    productivity:  str          # "productive" | "unproductive" | "neutral"
    category_name: Optional[str]
    color:         str


class AppUsageDetailTitle(BaseModel):
    title:         str
    duration_secs: int
    pct:           float


class AppUsageDetailUser(BaseModel):
    name:          str
    duration_secs: int
    pct:           float


class AppUsageDetailPoint(BaseModel):
    date:          str
    duration_secs: int


class AppUsageDetailOut(BaseModel):
    app_name: str
    titles:   list[AppUsageDetailTitle]  = []
    users:    list[AppUsageDetailUser]   = []
    usage:    list[AppUsageDetailPoint]  = []


class AppUsageListOut(BaseModel):
    items:       list[AppUsageItem]
    total_secs:  int
    total_apps:  int
    total_users: int


# ── Category Usage ────────────────────────────────────────────────────────────

class CategoryUsageItem(BaseModel):
    cat_id:        Optional[str]
    name:          str
    color:         str
    duration_secs: int
    user_count:    int
    pct:           float


class CategoryDetailActivity(BaseModel):
    app_name:      str
    duration_secs: int
    pct:           float


class CategoryDetailUser(BaseModel):
    name:          str
    email:         str
    duration_secs: int
    pct:           float


class CategoryUsageDetailOut(BaseModel):
    cat_id:     Optional[str]
    name:       str
    color:      str
    total_secs: int
    activities: list[CategoryDetailActivity] = []
    users:      list[CategoryDetailUser]     = []


class CategoryUsageListOut(BaseModel):
    items:       list[CategoryUsageItem]
    total_secs:  int
    total_users: int
