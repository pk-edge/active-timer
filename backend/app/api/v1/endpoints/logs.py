"""
GET /v1/logs — company-wide activity feed for the dashboard Logs page.
Returns the most recent events across all devices belonging to the admin's company.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import get_db, get_current_admin, require
from app.core.config import get_settings
from app.core.scoping import visible_device_ids, ALL
from app.core.search import build_search
from app.db.models import Employee
from app.schemas.event import LogsPageOut

router = APIRouter()
settings = get_settings()

# Hard ceiling on the per-device LATERAL sub-limit computed for pagination
# correctness (see _pagination_per_device). Bounds worst-case query cost when a
# caller pages very deep into a very large fleet — beyond this, pagination
# quietly falls back to fairness-only representation (see the function's
# docstring) rather than letting a single request fan out unbounded work.
#
# Kept deliberately small (not the 500 default per_device could otherwise grow
# towards): on the fleet-wide path this value is what the LATERAL executes
# PER VISIBLE DEVICE, so worst case cost is (visible_devices × this). At the
# simulator's default fleet size of 2000 devices, 1000 already means up to 2M
# candidate rows funnelled through OFFSET/LIMIT for one request — high enough
# to serve any page a human would actually click to, low enough that a crafted
# `offset` can't turn one API call into an unbounded table scan.
_MAX_PAGINATION_PER_DEVICE = 1000


def _require_uuid(value: str, param: str) -> str:
    """400 on malformed ids instead of letting CAST(:x AS uuid) 500 in the DB."""
    try:
        return str(_uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {param}")


def _pagination_per_device(per_device: Optional[int], offset: int, limit: int) -> int:
    """How many of each visible device's most-recent (filtered) events to pull
    into the fleet-wide merge candidate pool for the LATERAL join.

    Correctness argument: any row that belongs in the globally-sorted top
    (offset + limit) events across the WHOLE fleet must, by pigeonhole, also be
    within THAT SAME device's own top (offset + limit) most-recent events — a
    row's rank among just its own device's events can never exceed its rank
    among the whole fleet's events. So capping each device's contribution to
    the merge at (offset + limit + 1) rows is sufficient to guarantee the
    resulting page is complete, correctly ordered, and free of gaps. The "+1"
    is the same one-row over-fetch used at the outer LIMIT to derive
    `has_more` cheaply (see get_logs) — it must be present here too, or a
    device whose only remaining-page row lands exactly on the boundary could
    be dropped from the candidate pool before the outer query ever sees it.

    `settings.LOGS_PER_DEVICE_DEFAULT` is kept as a floor so tiny pages (e.g.
    limit=1) don't collapse fairness spread to almost nothing.

    If the caller passes an explicit `per_device`, that's an intentional
    fairness knob — "never let one device contribute more than N rows to this
    feed" — and is honored as-is. That means deep pagination can legitimately
    run out before the real fleet history does once
    (visible_devices × per_device) rows have been exhausted; that tradeoff
    already existed before this change, it's just explicit now instead of
    being silently the only option.
    """
    if per_device is not None:
        return per_device
    return min(
        max(settings.LOGS_PER_DEVICE_DEFAULT, offset + limit + 1),
        _MAX_PAGINATION_PER_DEVICE,
    )


@router.get("", response_model=LogsPageOut)
async def get_logs(
    since:       Optional[int] = Query(default=None),
    until:       Optional[int] = Query(default=None),
    device_id:   Optional[str] = Query(default=None),
    employee_id: Optional[str] = Query(default=None),
    event_type:  Optional[str] = Query(default=None),
    search:      Optional[str] = Query(default=None),
    limit:       int = Query(default=500, le=2000),
    # Capped well below what a human clicking "Next" could ever reach (a UI
    # session would need ~400 page loads at 50/page to hit this) but far below
    # the previous 200_000: on the fleet-wide path `offset` directly drives
    # `_pagination_per_device`'s growth, so an uncapped offset is an uncapped
    # LATERAL fan-out per visible device — see that function's docstring.
    offset:      int = Query(default=0, ge=0, le=20_000),
    per_device:  Optional[int] = Query(default=None, ge=1, le=500),
    admin: Employee = Depends(require("logs", "view")),
    db: AsyncSession = Depends(get_db),
):
    """Company-wide activity feed with PER-DEVICE FAIRNESS and pagination.

    A flat global `ORDER BY timestamp DESC LIMIT n` lets the busiest devices
    monopolize the newest rows, so quieter-but-online devices disappear from the
    page once the fleet grows past ~100 (the reported "logs stop, device still
    online" bug). Instead we take the most-recent `per_device` events for EACH
    visible device via a LATERAL join (index-backed by idx_events_device_ts),
    then merge, order by recency, and page with `offset`/`limit`.

    When `device_id` is given the feed is already a single device, so fairness is
    moot and we use the simpler flat query.

    Pagination: `offset`/`limit` page a feed ordered by `(timestamp DESC, id
    DESC)`. `timestamp` alone is not unique — many events land in the same
    second — so without the `id` tiebreaker the same row could appear on two
    pages, or a row could be skipped between them; both orderings are stable
    because `id` never changes. On the fleet-wide path, the per-device LATERAL
    sub-limit is widened to keep pace with how deep the caller is paging (see
    `_pagination_per_device`), so a wide date range no longer silently caps the
    visible feed to a tiny fraction of what matches — the original bug report.

    Response shape: `{"items": [...], "has_more": bool, "offset": int,
    "limit": int}`. `has_more` is derived by fetching one row past `limit`
    rather than running a separate `COUNT(*)` — an exact total over
    `activity_events` (tens of millions of rows, often over an open-ended date
    range) would mean a slow scan on every single page load. This is a
    deliberate, documented departure from an exact total: the UI can say
    "more available" / enable Next, but not "record N of exactly M". The
    dashboard's Activity Logs page is the endpoint's only caller (grepped:
    `getLogs` in `dashboard/src/lib/api.ts`), so moving from a bare list to
    this envelope is not a breaking change for any other consumer.

    Search: `search` runs the shared `app/core/search.py::build_search()`
    helper over `app_name`/`window_title`/`url`/employee `name`/device
    `hostname`, narrowing the FULL matching range server-side BEFORE
    offset/limit pages through it — not a client-side filter over whatever
    page happens to be in hand. It is applied inside the per-device LATERAL
    subquery on the fleet-wide path (not after it), so an older matching
    event on a busy device is not discarded by the per-device recency cap
    before search ever sees it. Ordering is left untouched — this is a
    chronological feed, so only `build_search()`'s WHERE half is used, never
    its relevance `ORDER BY`.
    """
    # Over-fetch by one row past `limit` so we can tell whether another page
    # exists without a separate COUNT query (see docstring above).
    fetch_limit = limit + 1
    params: dict = {"company_id": str(admin.company_id), "limit": fetch_limit, "offset": offset}
    ev_clauses: list[str] = []   # filters on the per-device event subquery (ae.*)
    dev_clauses = ["d.company_id = :company_id", "d.is_active = TRUE"]

    # Data scope: restrict to devices the user may see (bound param, not interpolated).
    vids = await visible_device_ids(admin, db)
    if vids is not ALL:
        if not vids:
            return {"items": [], "has_more": False, "offset": offset, "limit": limit}
        dev_clauses.append("d.id = ANY(:vids)")
        params["vids"] = [str(i) for i in vids]

    if since is not None:
        ev_clauses.append("ae.timestamp >= to_timestamp(:since)")
        params["since"] = since
        # Skip-dead-devices prefilter: a device whose last_seen predates the
        # window start cannot have events in it, so its LATERAL probe would be
        # a guaranteed-empty index descent. On a 2000-device fleet this drops
        # most probes for narrow windows.
        dev_clauses.append("(d.last_seen IS NULL OR d.last_seen >= to_timestamp(:since))")
    else:
        default_since = datetime.now(timezone.utc) - timedelta(hours=48)
        ev_clauses.append("ae.timestamp >= :default_since")
        params["default_since"] = default_since
        dev_clauses.append("(d.last_seen IS NULL OR d.last_seen >= :default_since)")

    if until is not None:
        ev_clauses.append("ae.timestamp < to_timestamp(:until)")
        params["until"] = until

    if employee_id:
        dev_clauses.append("d.employee_id = CAST(:employee_id AS uuid)")
        params["employee_id"] = _require_uuid(employee_id, "employee_id")

    if event_type:
        ev_clauses.append("ae.event_type = :event_type")
        params["event_type"] = event_type

    # Free-text search over app/window/url/employee-name/hostname. Deliberately
    # appended to `ev_clauses` — NOT applied as a post-merge filter on the
    # outer query — so that on the fleet-wide path it lands INSIDE the LATERAL
    # subquery, alongside since/until/event_type. That ordering matters: the
    # LATERAL's own `LIMIT :per_device` caps each device's contribution to the
    # merge candidate pool, and if search ran after that cap, an older
    # matching event on a busy device could already have been discarded by the
    # recency cap before search ever saw it (a real match silently missed).
    # Because `d.hostname` and `e.name` are already joined into scope before
    # the `JOIN LATERAL` in the fleet-wide query, referencing them inside the
    # LATERAL's own WHERE is valid (LATERAL may reference any preceding
    # FROM-list item) — see `_pagination_per_device`'s pigeonhole argument,
    # which holds unchanged here because it only depends on `ev_clauses` (now
    # including this) being applied identically to both the per-device
    # candidate LIMIT and the fleet-wide ordering/paging.
    #
    # Ordering is intentionally left alone: this is a chronological activity
    # feed, not a relevance-ranked people-search, so only `sc.where` is used —
    # `sc.order_by` is discarded and the existing `timestamp DESC, id DESC`
    # ordering applies whether or not a search term is given.
    sc = build_search(
        search,
        ["ae.app_name", "ae.window_title", "ae.url", "e.name", "d.hostname"],
        prefix="lsearch",
    )
    if sc.where:
        ev_clauses.append(sc.where)
        params.update(sc.params)

    if device_id:
        # Single device → fairness is irrelevant; flat recency feed.
        dev_clauses.append("d.id = CAST(:device_id AS uuid)")
        params["device_id"] = _require_uuid(device_id, "device_id")
        ev_sql = (" AND " + " AND ".join(ev_clauses)) if ev_clauses else ""
        query = text(f"""
            SELECT
                ae.id, ae.event_type, ae.app_name, ae.window_title,
                ae.url, ae.duration, ae.timestamp,
                COALESCE(EXTRACT(EPOCH FROM ae.timestamp)::bigint, 0) AS timestamp_unix,
                d.id       AS device_id,
                d.hostname AS hostname,
                e.id       AS employee_id,
                e.name     AS employee_name
            FROM activity_events ae
            JOIN devices d ON d.id = ae.device_id
            LEFT JOIN employees e ON e.id = d.employee_id
            WHERE {" AND ".join(dev_clauses)}{ev_sql}
            ORDER BY ae.timestamp DESC, ae.id DESC
            OFFSET :offset
            LIMIT :limit
        """)
    else:
        # Whole fleet → top-N events PER device via LATERAL, then merge + page.
        params["per_device"] = _pagination_per_device(per_device, offset, limit)
        ev_sql = (" AND " + " AND ".join(ev_clauses)) if ev_clauses else ""
        query = text(f"""
            SELECT
                ev.id, ev.event_type, ev.app_name, ev.window_title,
                ev.url, ev.duration, ev.timestamp,
                COALESCE(EXTRACT(EPOCH FROM ev.timestamp)::bigint, 0) AS timestamp_unix,
                d.id       AS device_id,
                d.hostname AS hostname,
                e.id       AS employee_id,
                e.name     AS employee_name
            FROM devices d
            LEFT JOIN employees e ON e.id = d.employee_id
            JOIN LATERAL (
                SELECT ae.id, ae.event_type, ae.app_name, ae.window_title,
                       ae.url, ae.duration, ae.timestamp
                FROM activity_events ae
                WHERE ae.device_id = d.id{ev_sql}
                ORDER BY ae.timestamp DESC, ae.id DESC
                LIMIT :per_device
            ) ev ON TRUE
            WHERE {" AND ".join(dev_clauses)}
            ORDER BY ev.timestamp DESC, ev.id DESC
            OFFSET :offset
            LIMIT :limit
        """)

    result = await db.execute(query, params)

    rows = result.fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [
            {
                "id":             str(r.id),
                "event_type":     r.event_type,
                "app_name":       r.app_name,
                "window_title":   r.window_title,
                "url":            r.url,
                "duration":       r.duration,
                "timestamp":      r.timestamp.isoformat() if r.timestamp else None,
                "timestamp_unix": r.timestamp_unix,
                "device_id":      str(r.device_id),
                "hostname":       r.hostname,
                "employee_id":    str(r.employee_id) if r.employee_id else None,
                "employee_name":  r.employee_name,
            }
            for r in rows
        ],
        "has_more": has_more,
        "offset":   offset,
        "limit":    limit,
    }
