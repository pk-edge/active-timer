# Technology & AI query performance — design

**Date:** 2026-09-21
**Status:** implemented — see "Results" at the end

## Problem

The four Technology & AI pages are slow whenever a filter is applied, and the
device filter added in `9ea97b2` made it visible. Measured on a 72M-row / 22 GB
`activity_events` copy:

| Query shape | Latency |
|---|---|
| 1 device, 1 day | 82 ms |
| 1 device, 30 days | **5,954 ms** |
| 50 devices, 30 days | **171,138 ms** |

## Root causes

1. **No device-leading covering index.** `idx_events_device_ts
   (device_id, timestamp DESC)` is not covering, so reading `app_name` /
   `duration` / `event_type` forces one heap fetch per row — 33,806 random heap
   blocks for 34K rows. The same rows via index-only scan take **44 ms vs
   5,954 ms**: a 135× penalty that is pure heap I/O. The existing covering index
   `idx_events_window_covering` leads with `timestamp`, so it cannot serve a
   `device_id = X` predicate. On the 50-device query the planner nested-looped
   over all 2,003 company devices — hence 171 s.
2. **The `tech_daily_*_cache` tables are keyed `(company_id, day, …)`** with no
   device/group/employee dimension, so every filtered request — and every
   request from a non-`all`-scoped admin — bypasses them by construction.
3. **Stale-while-revalidate is discarded.** All 11 `cache_get` call sites in
   `tech_utilization.py` do `_cached, _ = await cache_get(...)`, dropping the
   `is_stale` flag. Nothing ever refreshes in the background, so every
   `STALE_ANALYTICS` (600 s) some user pays the full recompute. `home.py`
   already implements the correct pattern (`_resp_bg_keys`).
4. **No single-flight.** `/tech` mounts 10 SWR calls and a filter change re-keys
   them at once, so a cold key is recomputed N times concurrently.
5. **`/filters/{groups,users,devices}` never touch Redis** — three Postgres
   queries per page mount, serializing 2,003 devices each time, across 4 pages.
6. `_productivity_map()` runs a query on every single request.

## Decision

**Approach A: one new covering index + a full Redis tier.** Rejected
alternatives: a per-device rollup table only (leaves detail drill-downs slow,
heavier worker), and Redis-only (first view of any filter combination still
costs 6 s–171 s).

### 1. Indexes — net +1 on `activity_events`

```sql
CREATE INDEX CONCURRENTLY idx_events_window_device_covering
    ON activity_events (device_id, "timestamp", app_name, duration)
    WHERE event_type = 'window_change' AND app_name IS NOT NULL;
```

Measured effect: **5,954 → 173 ms** (34×) and **171,138 → 3,597 ms** (48×).

Key columns rather than `INCLUDE`: both variants measured at exactly 4295 MB,
so the simpler form that matches `idx_events_window_covering`'s existing style
wins.

**WAL cost, explicitly.** `window_change` is ~97.6% of rows, so this index is
touched by essentially every insert: 5 → 6 index updates on the hot path
(~+25% index WAL). This is the same hot path migration 028 regressed on, but
028 was a 14 → 5 reduction after a 3.6:1 index-to-heap ratio; this is a single
targeted addition, and unlike the nine indexes 028 dropped for having **zero**
scans, this one serves the dominant filtered read shape. If checkpoint chaining
reappears, this index is the first thing to drop — the endpoints stay correct
without it, only slower.

The URL half is fixed at **net-zero index count** by upgrading the existing
`idx_events_urls (device_id, timestamp DESC) WHERE event_type = 'url_visit'` to
`(device_id, "timestamp", url)` with the same predicate — a strict superset of
what it serves today, so no current call site regresses.

### 2. Redis tier

- **Stale-while-revalidate** at all 11 tech `cache_get` sites, via a shared
  `_cached_or_compute()` helper that follows `home.py`'s `_resp_bg_keys`
  pattern: serve fresh; serve stale and refresh in a background task on its own
  `AsyncSessionLocal`; compute only on a true miss.
- **Single-flight** via a Redis `SET NX` lock so concurrent cold misses across
  workers collapse to one computation; losers wait briefly and re-read.
- **Settled-window TTLs.** A range whose `until` is older than
  `tech_cache.RECENT_DAYS` (3) is one the cache worker itself no longer
  rescans, so it is safe to hold far longer: 30 min fresh / 6 h stale instead
  of 2 min / 10 min. This is most of the date-filter pain — historical ranges
  currently expire every 10 minutes and get recomputed from scratch.
- **Cache the filter lookups** (`/filters/groups`, `/filters/users`,
  `/filters/devices`) in Redis.
- **Cache `_productivity_map`** per company, evicted by the category/rule
  mutation endpoints that already exist.

### 3. Explicitly deferred

The per-device daily rollup (`tech_daily_device_app_cache`). The index takes the
headline case to 173 ms; the rollup should only be built if wide-range *group*
views are still painful afterwards. Revisit with measurements, not assumptions.

## Testing

- Regression: the existing 231-test suite, plus `test_tech_device_filter.py`
  (filter correctness must not change — this is a pure performance change).
- New: cache-behaviour tests for SWR (stale serves immediately + schedules a
  refresh), single-flight (concurrent misses compute once), settled-window TTL
  selection, and filter-lookup caching.
- Verification: re-run the three measured query shapes and confirm index-only
  scans in `EXPLAIN`.


## Results (measured after implementation)

Migration 036 applied to the 72M-row reference DB, `ANALYZE` run, plans confirmed
as index-only scans.

| Query shape | Before | After | |
|---|---|---|---|
| 1 device, 1 day | 82 ms | 24 ms | 3.4× |
| 1 device, 30 days | 5,954 ms | 92 ms | **65×** |
| 50 devices, 30 days | 171,138 ms | 3,918 ms | **44×** |

End-to-end through the API (`/v1/tech/apps`, 30-day device-filtered):

| | Latency |
|---|---|
| cold (Redis flushed) | 175 ms |
| warm (Redis hit) | 2.4 ms |
| 8 concurrent cold requests, wall clock | 76 ms (single-flight collapsed them) |
| `/filters/devices`, 2,003 devices | 108 ms cold → 7 ms warm |

Index footprint: `idx_events_window_device_covering` 4,295 MB. The URL side was
net-zero — `idx_events_urls` was replaced by `idx_events_url_device_covering`.

Test suite: 244 passed, 0 failed (231 baseline + 12 `test_analytics_cache.py`
+ 1 `test_tech_device_filter.py`).

Deferred as planned: the per-device daily rollup table. The index removed the
need for it on the single-device path; revisit only if wide-range *group* views
(still ~3.9 s cold, ~2 ms warm) prove painful in practice.
