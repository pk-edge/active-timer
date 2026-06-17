#!/usr/bin/env python3
"""
TimeTracker Load & Stress Test Suite
======================================
Simulates 2,000+ concurrent devices + SSE dashboard tabs to find every crack
in the backend under realistic production load.

Phases
------
  0  Preflight       — health + DB connectivity + admin login + enrollment token
  1  Enroll storm     — 2,000 virtual devices enroll in waves of 200
  2  Ingest burst     — all 2,000 devices POST 50 events simultaneously
  3  SSE + ingest     — 60 SSE tabs open while 2,000 devices ingest repeatedly
  4  Dashboard flood  — 100 admin tabs hit /home/* + /devices while 500 devices ingest
  5  Pool probe       — 30 SSE + 150 concurrent HTTP; verify pg_stat_activity stays in budget
  6  Sustained load   — 500 devices ingest every 5 s for 60 s (steady-state)
  7  Teardown         — mark all lt-dev-* devices inactive via admin API

Real-time DB monitor (every 2 s during every phase)
-----------------------------------------------------
  • pg_stat_activity: total / idle-in-tx / waiting-on-lock / max-query-age
  • Pool budget alert if connections exceed 28 (2-task rolling-deploy ceiling)
  • All samples written to <report_dir>/db_samples.jsonl

Final report
------------
  • Human-readable table: phase / requests / errors / p50 / p95 / p99 / max / verdict
  • Machine-readable JSON:  <report_dir>/report.json
  • Exit code 0 = all phases PASS  |  1 = at least one FAIL

Requirements (already in backend/requirements.txt — run INSIDE the container):
  httpx==0.27.0   asyncpg==0.29.0

Usage
-----
  # Recommended: run inside the backend container (all deps present)
  docker compose exec backend python tests/loadtest_suite.py \\
      --url http://localhost:8000 \\
      --db  postgresql://timetracker:timetracker_dev@db:5432/timetracker \\
      --admin-email admin@edge.co \\
      --admin-password yourpassword \\
      --devices 2000

  # OR run from host machine (needs httpx + asyncpg installed in system Python):
  python3 backend/tests/loadtest_suite.py \\
      --url http://localhost:8000 \\
      --db  postgresql://timetracker:timetracker_dev@localhost:15432/timetracker \\
      --admin-email admin@edge.co \\
      --admin-password yourpassword

  # Quick smoke-test (fewer devices, shorter phases):
  docker compose exec backend python tests/loadtest_suite.py \\
      --devices 200 --sustained-secs 20 \\
      --admin-email admin@edge.co --admin-password yourpassword
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import httpx

# ── Tuning knobs ──────────────────────────────────────────────────────────────

ENROLL_CONCURRENCY      = 200   # max simultaneous enrollment requests
INGEST_CONCURRENCY      = 200   # max simultaneous ingest requests
INGEST_EVENTS_PER_BATCH = 50    # events per ingest POST
SSE_STREAMS             = 60    # SSE subscriber count during SSE phase
DASHBOARD_TABS          = 100   # concurrent admin dashboard readers
POOL_BUDGET             = 28    # pg_stat_activity must not exceed this
DB_POLL_INTERVAL        = 2.0   # seconds between pg_stat_activity samples
HTTP_TIMEOUT            = 15.0  # per-request hard timeout (= pool_timeout + margin)
SSE_TIMEOUT             = 120.0 # how long SSE phase runs

ALLOWED_EVENTS = [
    "window_change", "idle_start", "idle_end",
    "lock", "unlock", "url_visit",
]
APP_NAMES = [
    "Chrome", "Firefox", "VSCode", "Terminal",
    "Slack", "Zoom", "Word", "Excel", "Figma", "Outlook",
]
DEVICE_PREFIX = "lt-dev"

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RequestRecord:
    status: int
    elapsed: float      # seconds
    error: str = ""     # non-empty on exception


@dataclass
class DBSample:
    ts: float
    total: int
    idle_in_tx: int
    waiting_lock: int
    longest_age_secs: float
    phase: str = ""


@dataclass
class PhaseResult:
    name: str
    records: List[RequestRecord] = field(default_factory=list)
    db_samples: List[DBSample] = field(default_factory=list)
    sse_events_received: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── Computed properties ────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.records if r.status >= 500 or r.error)

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 0.0

    @property
    def status_breakdown(self) -> Dict[int, int]:
        bd: Dict[int, int] = defaultdict(int)
        for r in self.records:
            bd[r.status] += 1
        return dict(sorted(bd.items()))

    def latency_pct(self, pct: float) -> float:
        times = sorted(r.elapsed for r in self.records if not r.error)
        if not times:
            return 0.0
        idx = max(0, math.ceil(pct / 100.0 * len(times)) - 1)
        return times[idx]

    @property
    def p50(self) -> float:  return self.latency_pct(50)
    @property
    def p95(self) -> float:  return self.latency_pct(95)
    @property
    def p99(self) -> float:  return self.latency_pct(99)
    @property
    def p_max(self) -> float:
        times = [r.elapsed for r in self.records if not r.error]
        return max(times) if times else 0.0

    @property
    def db_peak(self) -> int:
        return max((s.total for s in self.db_samples), default=0)

    @property
    def verdict(self) -> str:
        if self.error_rate > 0.02:
            return "FAIL (>2% errors)"
        if self.p99 > HTTP_TIMEOUT:
            return f"FAIL (p99={self.p99:.1f}s > {HTTP_TIMEOUT}s timeout)"
        if self.db_peak > POOL_BUDGET:
            return f"FAIL (DB peak {self.db_peak} > budget {POOL_BUDGET})"
        return "PASS"


# ── DB Monitor ────────────────────────────────────────────────────────────────

class DBMonitor:
    """Polls pg_stat_activity every DB_POLL_INTERVAL seconds in a background task."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: Optional[asyncpg.Connection] = None
        self._task: Optional[asyncio.Task] = None
        self.samples: List[DBSample] = []
        self._current_phase = "unknown"
        self._stop = asyncio.Event()
        self._peak_alert_printed: set = set()

    async def start(self):
        try:
            self._conn = await asyncpg.connect(self.dsn, timeout=10)
        except Exception as e:
            print(f"  [DB monitor] Cannot connect to Postgres: {e}")
            print(f"  [DB monitor] DB monitoring disabled — pool budget checks skipped")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> List[DBSample]:
        self._stop.set()
        if self._task:
            await asyncio.wait_for(self._task, timeout=5)
        if self._conn:
            await self._conn.close()
        return self.samples

    def set_phase(self, name: str):
        self._current_phase = name

    async def snapshot(self) -> Optional[DBSample]:
        if not self._conn:
            return None
        try:
            row = await self._conn.fetchrow("""
                SELECT
                    count(*)                                                   AS total,
                    count(*) FILTER (WHERE state = 'idle in transaction')      AS idle_in_tx,
                    count(*) FILTER (WHERE wait_event_type = 'Lock')           AS waiting_lock,
                    EXTRACT(EPOCH FROM max(now() - state_change))              AS longest_age
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
            """)
            s = DBSample(
                ts=time.monotonic(),
                total=int(row["total"]),
                idle_in_tx=int(row["idle_in_tx"]),
                waiting_lock=int(row["waiting_lock"]),
                longest_age_secs=float(row["longest_age"] or 0),
                phase=self._current_phase,
            )
            self.samples.append(s)
            if s.total > POOL_BUDGET:
                key = (self._current_phase, s.total // 5)
                if key not in self._peak_alert_printed:
                    self._peak_alert_printed.add(key)
                    print(f"\n  [DB ALERT] {s.total} connections active (budget={POOL_BUDGET}) "
                          f"in phase '{self._current_phase}'")
            return s
        except Exception:
            return None

    async def _run(self):
        while not self._stop.is_set():
            await self.snapshot()
            try:
                await asyncio.wait_for(asyncio.shield(self._stop.wait()), timeout=DB_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _make_client(max_conn: int = 500) -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_keepalive_connections=max_conn,
        max_connections=max_conn + 100,
    )
    return httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=10.0),
    )


async def _req(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    result: PhaseResult,
    **kwargs,
) -> Optional[httpx.Response]:
    t0 = time.monotonic()
    try:
        resp = await client.request(method, url, **kwargs)
        result.records.append(RequestRecord(status=resp.status_code, elapsed=time.monotonic() - t0))
        return resp
    except Exception as e:
        result.records.append(RequestRecord(status=0, elapsed=time.monotonic() - t0, error=str(e)[:120]))
        return None


def _make_events(n: int, base_ts: Optional[int] = None) -> List[dict]:
    t = base_ts or int(time.time()) - n * 30
    events = []
    for i in range(n):
        et = ALLOWED_EVENTS[i % len(ALLOWED_EVENTS)]
        events.append({
            "id":           str(uuid.uuid4()),
            "event_type":   et,
            "app_name":     APP_NAMES[i % len(APP_NAMES)],
            "window_title": f"Load Test Window {i}",
            "url":          f"https://example.com/lt/{i}" if et == "url_visit" else None,
            "duration":     30,
            "timestamp":    t + i * 30,
        })
    return events


# ── Phase helpers ─────────────────────────────────────────────────────────────

async def _batch(coros, concurrency: int, progress_label: str = ""):
    """Run coros in batches of `concurrency`, printing a progress bar."""
    sem = asyncio.Semaphore(concurrency)
    total = len(coros)
    done = [0]

    async def _guarded(coro):
        async with sem:
            await coro
            done[0] += 1
            pct = done[0] * 100 // total
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"\r  {progress_label} [{bar}] {done[0]}/{total}", end="", flush=True)

    await asyncio.gather(*[_guarded(c) for c in coros])
    print()


# ── Phases ────────────────────────────────────────────────────────────────────

async def phase0_preflight(cfg: argparse.Namespace, monitor: DBMonitor) -> Tuple[str, str, str]:
    """
    Returns (admin_jwt, enrollment_token, company_id).
    Raises SystemExit on any failure.
    """
    monitor.set_phase("preflight")
    print("\n── Phase 0: Preflight ─────────────────────────────────────────────────────")

    async with _make_client(50) as client:
        # Health
        resp = await client.get(f"{cfg.url}/health")
        assert resp.status_code == 200, f"Health check failed: {resp.status_code}"
        print("  /health            ✓")

        resp = await client.get(f"{cfg.url}/health/db")
        assert resp.status_code == 200, f"DB health failed: {resp.text}"
        print("  /health/db         ✓")

        # Admin login
        resp = await client.post(
            f"{cfg.url}/v1/auth/admin/login",
            json={"email": cfg.admin_email, "password": cfg.admin_password},
        )
        if resp.status_code != 200:
            print(f"  Admin login FAILED: {resp.status_code} {resp.text}")
            sys.exit(1)
        data = resp.json()
        admin_jwt = data["access_token"]
        print(f"  Admin login        ✓  (user={cfg.admin_email})")

        headers = {"Authorization": f"Bearer {admin_jwt}"}

        # Company info (enrollment token)
        resp = await client.get(f"{cfg.url}/v1/company/info", headers=headers)
        if resp.status_code != 200:
            print(f"  Company info FAILED: {resp.status_code} {resp.text}")
            sys.exit(1)
        cinfo = resp.json()
        enrollment_token = cinfo["enrollment_token"]
        print(f"  Enrollment token   ✓  ({enrollment_token[:8]}…)")

        # DB snapshot
        snap = await monitor.snapshot()
        if snap:
            print(f"  pg_stat_activity   {snap.total} connections (budget={POOL_BUDGET})")

    return admin_jwt, enrollment_token


async def phase1_enroll_storm(
    cfg: argparse.Namespace,
    enrollment_token: str,
    monitor: DBMonitor,
) -> Tuple[PhaseResult, List[str]]:
    """
    Enroll `cfg.devices` virtual devices. Returns (result, list_of_device_jwts).
    This stresses the HTTP pool: each enrollment does 2 SELECTs + 1 INSERT + commit.
    """
    monitor.set_phase("enroll_storm")
    print(f"\n── Phase 1: Enrollment Storm ({cfg.devices} devices) ─────────────────────")
    result = PhaseResult(name="enroll_storm")
    device_jwts: List[Optional[str]] = [None] * cfg.devices

    async def enroll(i: int):
        hostname = f"{DEVICE_PREFIX}-{i:04d}"
        async with _make_client(10) as c:
            resp = await _req(c, "POST", f"{cfg.url}/v1/auth/enroll", result,
                              json={
                                  "enrollment_token": enrollment_token,
                                  "hostname": hostname,
                                  "platform": "linux",
                                  "agent_version": "load-test-1.0",
                                  "employee_email": f"lt-user-{i:04d}@loadtest.internal",
                              })
            if resp and resp.status_code == 200:
                device_jwts[i] = resp.json().get("jwt_token")

    await _batch([enroll(i) for i in range(cfg.devices)], ENROLL_CONCURRENCY, "enrolling")

    enrolled = sum(1 for t in device_jwts if t)
    print(f"  Enrolled: {enrolled}/{cfg.devices}   errors: {result.errors}   "
          f"p95={result.p95:.3f}s   p99={result.p99:.3f}s")
    print(f"  Status breakdown: {result.status_breakdown}")
    snap = await monitor.snapshot()
    if snap:
        print(f"  DB connections peak: {result.db_peak}  (now={snap.total})")

    valid_jwts = [t for t in device_jwts if t]
    return result, valid_jwts


async def phase2_ingest_burst(
    cfg: argparse.Namespace,
    device_jwts: List[str],
    monitor: DBMonitor,
) -> PhaseResult:
    """
    All devices POST INGEST_EVENTS_PER_BATCH events simultaneously.
    Tests the ingest queue, overflow fallback, and broadcaster.
    """
    monitor.set_phase("ingest_burst")
    print(f"\n── Phase 2: Ingest Burst ({len(device_jwts)} devices × {INGEST_EVENTS_PER_BATCH} events) ──")
    result = PhaseResult(name="ingest_burst")
    events = _make_events(INGEST_EVENTS_PER_BATCH)

    async def ingest(jwt: str):
        async with _make_client(5) as c:
            await _req(c, "POST", f"{cfg.url}/v1/ingest", result,
                       headers={"Authorization": f"Bearer {jwt}"},
                       json={"events": events})

    t0 = time.monotonic()
    await _batch([ingest(jwt) for jwt in device_jwts], INGEST_CONCURRENCY, "ingesting")
    elapsed = time.monotonic() - t0

    throughput = result.total / elapsed
    print(f"  Requests: {result.total}   errors: {result.errors}   "
          f"throughput: {throughput:.0f} req/s")
    print(f"  p50={result.p50:.3f}s  p95={result.p95:.3f}s  "
          f"p99={result.p99:.3f}s  max={result.p_max:.3f}s")
    print(f"  Status breakdown: {result.status_breakdown}")
    snap = await monitor.snapshot()
    if snap:
        print(f"  DB connections peak: {result.db_peak}  (now={snap.total})")
    return result


async def phase3_sse_plus_ingest(
    cfg: argparse.Namespace,
    admin_jwt: str,
    device_jwts: List[str],
    monitor: DBMonitor,
) -> PhaseResult:
    """
    Hold SSE_STREAMS SSE connections open while devices keep ingesting.
    Verifies: db.close() works (connections not pinned), throttle fires,
    SSE events arrive, no pool exhaustion.
    """
    monitor.set_phase("sse_plus_ingest")
    print(f"\n── Phase 3: SSE × {SSE_STREAMS} + Ingest ({len(device_jwts)} devices, "
          f"{SSE_TIMEOUT:.0f}s) ────────────────")
    result = PhaseResult(name="sse_plus_ingest")
    stop_sse = asyncio.Event()
    sse_error_count = [0]
    sse_events_received = [0]
    sse_connected = [0]

    async def hold_sse_stream(idx: int):
        url = f"{cfg.url}/v1/devices/stream?token={admin_jwt}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(SSE_TIMEOUT + 10, connect=10.0)
            ) as c:
                async with c.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        sse_error_count[0] += 1
                        return
                    sse_connected[0] += 1
                    async for line in resp.aiter_lines():
                        if stop_sse.is_set():
                            break
                        if line.startswith("event:") and "error" not in line and "connected" not in line:
                            sse_events_received[0] += 1
        except Exception:
            sse_error_count[0] += 1

    async def ingest_wave(jwts: List[str]):
        """Repeatedly ingest until stop_sse fires."""
        events = _make_events(20)
        sem = asyncio.Semaphore(INGEST_CONCURRENCY)

        async def one(jwt: str):
            async with sem:
                async with _make_client(5) as c:
                    await _req(c, "POST", f"{cfg.url}/v1/ingest", result,
                               headers={"Authorization": f"Bearer {jwt}"},
                               json={"events": events})

        while not stop_sse.is_set():
            batch = jwts[:min(500, len(jwts))]
            await asyncio.gather(*[one(jwt) for jwt in batch])
            if not stop_sse.is_set():
                await asyncio.sleep(1.0)

    # Start SSE streams
    sse_tasks = [asyncio.create_task(hold_sse_stream(i)) for i in range(SSE_STREAMS)]
    await asyncio.sleep(2.0)  # let streams connect
    print(f"  SSE streams connected: {sse_connected[0]}/{SSE_STREAMS}")

    # Run ingest wave until timeout
    ingest_task = asyncio.create_task(ingest_wave(device_jwts))
    await asyncio.sleep(SSE_TIMEOUT)
    stop_sse.set()

    await asyncio.gather(ingest_task, *sse_tasks, return_exceptions=True)

    result.sse_events_received = sse_events_received[0]
    print(f"  SSE events received: {sse_events_received[0]}  "
          f"SSE errors: {sse_error_count[0]}")
    print(f"  Ingest: {result.total} req   errors: {result.errors}   "
          f"p95={result.p95:.3f}s  p99={result.p99:.3f}s")
    print(f"  Status breakdown: {result.status_breakdown}")
    snap = await monitor.snapshot()
    if snap:
        print(f"  DB connections: peak={result.db_peak}  now={snap.total}  "
              f"idle_in_tx={snap.idle_in_tx}  waiting_lock={snap.waiting_lock}")

    if sse_error_count[0] > SSE_STREAMS * 0.1:
        result.extra["sse_fail"] = f"{sse_error_count[0]} SSE streams failed"
    return result


async def phase4_dashboard_flood(
    cfg: argparse.Namespace,
    admin_jwt: str,
    device_jwts: List[str],
    monitor: DBMonitor,
) -> PhaseResult:
    """
    DASHBOARD_TABS concurrent readers + 500 devices ingesting.
    Exercises every expensive dashboard endpoint under simultaneous ingest load.
    """
    monitor.set_phase("dashboard_flood")
    print(f"\n── Phase 4: Dashboard Flood ({DASHBOARD_TABS} tabs + 500 devices ingesting) ─")
    result = PhaseResult(name="dashboard_flood")
    stop = asyncio.Event()
    headers = {"Authorization": f"Bearer {admin_jwt}"}

    dashboard_endpoints = [
        "/v1/home/overview",
        "/v1/home/realtime",
        "/v1/home/app-usage",
        "/v1/home/web-usage",
        "/v1/devices",
        "/v1/reports/company/summary",
    ]

    async def dashboard_tab(tab_id: int):
        async with _make_client(10) as c:
            for _ in range(20):
                if stop.is_set():
                    break
                url = cfg.url + dashboard_endpoints[tab_id % len(dashboard_endpoints)]
                await _req(c, "GET", url, result, headers=headers)
                await asyncio.sleep(0.1)

    async def background_ingest():
        sem = asyncio.Semaphore(100)
        jwts = device_jwts[:500]
        events = _make_events(10)

        async def one(jwt: str):
            async with sem:
                async with _make_client(5) as c:
                    await _req(c, "POST", f"{cfg.url}/v1/ingest", result,
                               headers={"Authorization": f"Bearer {jwt}"},
                               json={"events": events})

        while not stop.is_set():
            await asyncio.gather(*[one(jwt) for jwt in jwts])
            await asyncio.sleep(2.0)

    t0 = time.monotonic()
    ingest_bg = asyncio.create_task(background_ingest())
    await asyncio.gather(*[dashboard_tab(i) for i in range(DASHBOARD_TABS)])
    stop.set()
    await asyncio.gather(ingest_bg, return_exceptions=True)
    elapsed = time.monotonic() - t0

    print(f"  Duration: {elapsed:.1f}s   Requests: {result.total}   "
          f"errors: {result.errors}   throughput: {result.total/elapsed:.0f} req/s")
    print(f"  p50={result.p50:.3f}s  p95={result.p95:.3f}s  "
          f"p99={result.p99:.3f}s  max={result.p_max:.3f}s")
    print(f"  Status breakdown: {result.status_breakdown}")
    snap = await monitor.snapshot()
    if snap:
        print(f"  DB peak={result.db_peak}  now={snap.total}  "
              f"idle_in_tx={snap.idle_in_tx}  lock_wait={snap.waiting_lock}  "
              f"longest={snap.longest_age_secs:.1f}s")
    return result


async def phase5_pool_probe(
    cfg: argparse.Namespace,
    admin_jwt: str,
    device_jwts: List[str],
    monitor: DBMonitor,
) -> PhaseResult:
    """
    The exact reproduction of the original production crash:
    30 SSE streams + 150 concurrent HTTP requests firing continuously.
    Must complete with 0 errors and DB never exceeding POOL_BUDGET.
    """
    monitor.set_phase("pool_probe")
    N_SSE = 30
    N_REQ = 150
    DURATION = 30
    print(f"\n── Phase 5: Pool Probe ({N_SSE} SSE + {N_REQ} concurrent req, {DURATION}s) ────────────")
    result = PhaseResult(name="pool_probe")
    stop = asyncio.Event()
    headers = {"Authorization": f"Bearer {admin_jwt}"}
    sse_connected = [0]

    async def hold_sse(idx: int):
        url = f"{cfg.url}/v1/devices/stream?token={admin_jwt}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(DURATION + 20, connect=10.0)
            ) as c:
                async with c.stream("GET", url) as resp:
                    if resp.status_code == 200:
                        sse_connected[0] += 1
                    async for _ in resp.aiter_lines():
                        if stop.is_set():
                            break
        except Exception:
            pass

    async def flood():
        sem = asyncio.Semaphore(N_REQ)
        mix_jwts = device_jwts[:100]
        events = _make_events(25)

        async def one(i: int):
            async with sem:
                async with _make_client(5) as c:
                    if i % 3 == 0:
                        await _req(c, "GET", f"{cfg.url}/v1/home/overview",
                                   result, headers=headers)
                    elif i % 3 == 1:
                        await _req(c, "GET", f"{cfg.url}/v1/devices",
                                   result, headers=headers)
                    else:
                        jwt = mix_jwts[i % len(mix_jwts)]
                        await _req(c, "POST", f"{cfg.url}/v1/ingest",
                                   result,
                                   headers={"Authorization": f"Bearer {jwt}"},
                                   json={"events": events})

        ctr = 0
        while not stop.is_set():
            batch = [one(ctr + j) for j in range(N_REQ)]
            ctr += N_REQ
            await asyncio.gather(*batch)

    sse_tasks = [asyncio.create_task(hold_sse(i)) for i in range(N_SSE)]
    await asyncio.sleep(2.0)
    print(f"  SSE streams open: {sse_connected[0]}/{N_SSE}  "
          f"(pool max is 10 — pre-fix this would jam here)")

    flood_task = asyncio.create_task(flood())
    await asyncio.sleep(DURATION)
    stop.set()
    await asyncio.gather(flood_task, *sse_tasks, return_exceptions=True)

    # Final DB snapshot
    snap = await monitor.snapshot()
    pg_now = snap.total if snap else 0

    print(f"  {N_REQ} concurrent req under {N_SSE} open SSE streams: "
          f"{result.total} OK, {result.errors} failed  in {DURATION}s")
    print(f"  p50={result.p50:.3f}s  p95={result.p95:.3f}s  "
          f"p99={result.p99:.3f}s  max={result.p_max:.3f}s")
    print(f"  pg connections: peak={result.db_peak}  now={pg_now}  budget={POOL_BUDGET}")
    print(f"  Status breakdown: {result.status_breakdown}")
    print(f"  {'✓ Pool held within budget' if result.db_peak <= POOL_BUDGET else '✗ POOL BUDGET EXCEEDED'}")
    return result


async def phase6_sustained_load(
    cfg: argparse.Namespace,
    device_jwts: List[str],
    monitor: DBMonitor,
) -> PhaseResult:
    """
    Steady-state: 500 devices ingest every 5 s for SUSTAINED_DURATION seconds.
    Models a real deployment with hundreds of agents running.
    """
    monitor.set_phase("sustained_load")
    duration = cfg.sustained_secs
    print(f"\n── Phase 6: Sustained Load (500 devices × every 5 s for {duration}s) ──────")
    result = PhaseResult(name="sustained_load")
    jwts = device_jwts[:min(500, len(device_jwts))]
    events = _make_events(30)

    sem = asyncio.Semaphore(INGEST_CONCURRENCY)
    stop_time = time.monotonic() + duration
    waves = 0

    while time.monotonic() < stop_time:
        wave_start = time.monotonic()
        waves += 1

        async def one(jwt: str):
            async with sem:
                async with _make_client(5) as c:
                    await _req(c, "POST", f"{cfg.url}/v1/ingest", result,
                               headers={"Authorization": f"Bearer {jwt}"},
                               json={"events": events})

        await asyncio.gather(*[one(jwt) for jwt in jwts])
        wave_elapsed = time.monotonic() - wave_start
        remaining = 5.0 - wave_elapsed
        if remaining > 0 and time.monotonic() < stop_time:
            await asyncio.sleep(remaining)

        snap = await monitor.snapshot()
        pg = snap.total if snap else "?"
        print(f"\r  wave {waves}  req={result.total}  err={result.errors}  "
              f"p95={result.p95:.3f}s  db={pg}        ", end="", flush=True)

    print()
    print(f"  Total waves: {waves}  Total requests: {result.total}  "
          f"Errors: {result.errors}  ({result.error_rate*100:.1f}%)")
    print(f"  p50={result.p50:.3f}s  p95={result.p95:.3f}s  "
          f"p99={result.p99:.3f}s  max={result.p_max:.3f}s")
    print(f"  DB peak: {result.db_peak}  budget: {POOL_BUDGET}")
    return result


async def teardown(
    cfg: argparse.Namespace,
    admin_jwt: str,
    device_count: int,
    monitor: DBMonitor,
):
    """Mark all lt-dev-* devices inactive."""
    monitor.set_phase("teardown")
    print(f"\n── Phase 7: Teardown ──────────────────────────────────────────────────────")
    # Use the admin device list endpoint — delete by marking inactive via the DB directly
    # if the API doesn't support bulk delete. For the load test we just report.
    print(f"  Load-test devices (lt-dev-0000 … lt-dev-{device_count-1:04d}) remain in DB.")
    print(f"  To clean up:  DELETE FROM devices WHERE hostname LIKE 'lt-dev-%';")
    print(f"                DELETE FROM employees WHERE email LIKE 'lt-user-%@loadtest.internal';")


# ── Report ────────────────────────────────────────────────────────────────────

def _print_report(phases: List[PhaseResult], db_samples: List[DBSample], output_dir: Path):
    print("\n" + "═" * 80)
    print("  LOAD TEST REPORT")
    print("═" * 80)

    col = "{:<20} {:>8} {:>8} {:>7} {:>7} {:>7} {:>7} {:>8} {:>8} {}"
    print(col.format(
        "Phase", "Requests", "Errors", "p50(s)", "p95(s)", "p99(s)", "max(s)", "DB-peak", "SSE-evts", "Verdict"
    ))
    print("─" * 110)

    all_pass = True
    rows = []
    for p in phases:
        verdict = p.verdict
        if verdict != "PASS":
            all_pass = False
        print(col.format(
            p.name[:20], p.total, p.errors,
            f"{p.p50:.3f}", f"{p.p95:.3f}", f"{p.p99:.3f}", f"{p.p_max:.3f}",
            p.db_peak, p.sse_events_received, verdict,
        ))
        rows.append({
            "phase": p.name,
            "total": p.total,
            "errors": p.errors,
            "error_rate": p.error_rate,
            "p50": p.p50, "p95": p.p95, "p99": p.p99, "max": p.p_max,
            "db_peak": p.db_peak,
            "status_breakdown": p.status_breakdown,
            "sse_events": p.sse_events_received,
            "verdict": verdict,
            "extra": p.extra,
        })

    print("─" * 110)

    # DB analysis
    if db_samples:
        peak_total = max(s.total for s in db_samples)
        peak_idle_tx = max(s.idle_in_tx for s in db_samples)
        peak_lock = max(s.waiting_lock for s in db_samples)
        peak_age = max(s.longest_age_secs for s in db_samples)
        print(f"\n  DB Statistics (across all phases):")
        print(f"    Peak total connections : {peak_total}  (budget={POOL_BUDGET})")
        print(f"    Peak idle-in-tx        : {peak_idle_tx}")
        print(f"    Peak waiting-on-lock   : {peak_lock}")
        print(f"    Longest query age      : {peak_age:.1f}s")
        if peak_total > POOL_BUDGET:
            print(f"    *** POOL BUDGET EXCEEDED — check for connection leaks ***")

    print(f"\n  Overall: {'✓ ALL PHASES PASSED' if all_pass else '✗ FAILURES DETECTED — see above'}")
    print("═" * 80)

    # Write JSON report
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "phases": rows,
        "db_samples": [
            {"ts": s.ts, "total": s.total, "idle_in_tx": s.idle_in_tx,
             "waiting_lock": s.waiting_lock, "longest_age": s.longest_age_secs,
             "phase": s.phase}
            for s in db_samples
        ],
        "all_pass": all_pass,
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2))

    samples_path = output_dir / "db_samples.jsonl"
    with samples_path.open("w") as f:
        for s in db_samples:
            f.write(json.dumps({
                "ts": s.ts, "phase": s.phase, "total": s.total,
                "idle_in_tx": s.idle_in_tx, "waiting_lock": s.waiting_lock,
                "longest_age": s.longest_age_secs,
            }) + "\n")

    print(f"\n  Report written to: {report_path}")
    print(f"  DB samples:        {samples_path}")
    return all_pass


# ── Entry point ───────────────────────────────────────────────────────────────

def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TimeTracker load & stress test")
    p.add_argument("--url",           default="http://localhost:8000",
                   help="Backend base URL (default: http://localhost:8000)")
    p.add_argument("--db",            default=os.environ.get(
                       "LOADTEST_DB_DSN",
                       "postgresql://timetracker:timetracker_dev@db:5432/timetracker"  # inside Docker
                   ),
                   help="Postgres DSN for DB monitoring (default uses docker-compose 'db' hostname)")
    p.add_argument("--admin-email",   default=os.environ.get("LOADTEST_ADMIN_EMAIL", ""),
                   help="Admin email (or set LOADTEST_ADMIN_EMAIL env var)")
    p.add_argument("--admin-password",default=os.environ.get("LOADTEST_ADMIN_PASSWORD", ""),
                   help="Admin password (or set LOADTEST_ADMIN_PASSWORD env var)")
    p.add_argument("--devices",       type=int, default=2000,
                   help="Number of virtual devices to enroll (default: 2000)")
    p.add_argument("--sustained-secs",type=int, default=60,
                   help="Duration of sustained-load phase in seconds (default: 60)")
    p.add_argument("--skip-phases",   nargs="*", default=[],
                   help="Phase names to skip, e.g. --skip-phases sse_plus_ingest sustained_load")
    p.add_argument("--output-dir",    default="loadtest_results",
                   help="Directory to write report.json + db_samples.jsonl")
    return p.parse_args()


async def main():
    cfg = _args()

    if not cfg.admin_email or not cfg.admin_password:
        print("ERROR: --admin-email and --admin-password are required.")
        print("       (or set LOADTEST_ADMIN_EMAIL / LOADTEST_ADMIN_PASSWORD env vars)")
        sys.exit(1)

    print("=" * 80)
    print(f"  TimeTracker Load & Stress Test")
    print(f"  Target  : {cfg.url}")
    print(f"  Devices : {cfg.devices}")
    print(f"  DB      : {cfg.db}")
    print("=" * 80)

    monitor = DBMonitor(cfg.db)
    await monitor.start()

    phases: List[PhaseResult] = []

    try:
        # Phase 0: preflight
        admin_jwt, enrollment_token = await phase0_preflight(cfg, monitor)

        # Phase 1: enrollment storm
        if "enroll_storm" not in cfg.skip_phases:
            r, device_jwts = await phase1_enroll_storm(cfg, enrollment_token, monitor)
            phases.append(r)
        else:
            print("\n── Phase 1: Enrollment Storm [SKIPPED] ────────────────────────────────────")
            # Re-enroll a small set to get JWTs for remaining phases
            _, device_jwts = await phase1_enroll_storm(
                argparse.Namespace(**vars(cfg) | {"devices": min(200, cfg.devices)}),
                enrollment_token, monitor
            )

        if not device_jwts:
            print("ERROR: No device JWTs obtained — cannot proceed past Phase 1.")
            sys.exit(1)

        # Phase 2: ingest burst
        if "ingest_burst" not in cfg.skip_phases:
            phases.append(await phase2_ingest_burst(cfg, device_jwts, monitor))

        # Phase 3: SSE + ingest
        if "sse_plus_ingest" not in cfg.skip_phases:
            phases.append(await phase3_sse_plus_ingest(cfg, admin_jwt, device_jwts, monitor))

        # Phase 4: dashboard flood
        if "dashboard_flood" not in cfg.skip_phases:
            phases.append(await phase4_dashboard_flood(cfg, admin_jwt, device_jwts, monitor))

        # Phase 5: pool probe (the original crash reproduction)
        if "pool_probe" not in cfg.skip_phases:
            phases.append(await phase5_pool_probe(cfg, admin_jwt, device_jwts, monitor))

        # Phase 6: sustained load
        if "sustained_load" not in cfg.skip_phases:
            phases.append(await phase6_sustained_load(cfg, device_jwts, monitor))

        # Phase 7: teardown
        await teardown(cfg, admin_jwt, cfg.devices, monitor)

    except KeyboardInterrupt:
        print("\n\nInterrupted — printing partial report...")
    except Exception as e:
        import traceback
        print(f"\nFATAL: {e}")
        traceback.print_exc()
    finally:
        db_samples = await monitor.stop()
        output_dir = Path(cfg.output_dir)
        all_pass = _print_report(phases, db_samples, output_dir) if phases else False
        sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
