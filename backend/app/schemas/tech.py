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
