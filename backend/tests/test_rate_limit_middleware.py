"""Global API rate limiter (pure-ASGI middleware) — policies, 429 shape, CORS,
identity, device bucket, exemptions, Redis-down fallback, streaming safety.

No DB: every app here is a tiny Starlette app wrapped in the real middleware
(plus CORSMiddleware outside it, exactly like main.py), and the real create_app()
is used for the CORS-on-429 check against a route that never touches the DB.
"""
import asyncio
import uuid

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.core.RateLimitMiddleware import RateLimitConfig, RateLimitMiddleware
from app.core.rate_limit import Bucket, MultiWindowLimiter
from app.core.security import create_admin_token, create_device_token

ORIGIN = "http://localhost:3000"


async def _ok(request):
    return JSONResponse({"ok": True})


async def _sse(request):
    async def gen():
        yield b"data: 1\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _build(cfg: RateLimitConfig, limiter: MultiWindowLimiter | None = None) -> Starlette:
    app = Starlette(routes=[
        Route("/v1/things", _ok, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/v1/reports/jobs", _ok, methods=["POST"]),
        Route("/v1/ingest", _ok, methods=["POST"]),
        Route("/v1/stream", _sse, methods=["GET"]),
        Route("/v1/devices/stream", _ok, methods=["GET"]),
        Route("/v1/auth/admin/login", _ok, methods=["POST"]),
        Route("/v1/auth/register", _ok, methods=["POST"]),
        Route("/v1/auth/enroll", _ok, methods=["POST"]),
        Route("/v1/setup/reset-admin", _ok, methods=["POST"]),
        Route("/v1/auth/google", _ok, methods=["GET"]),
        Route("/v1/auth/google/callback", _ok, methods=["GET"]),
        Route("/health", _ok, methods=["GET"]),
        Route("/docs", _ok, methods=["GET"]),
    ])
    app.add_middleware(RateLimitMiddleware, config=cfg, limiter=limiter)
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    return app


def _client(app, ip="10.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://t")


def _admin(user=None, company=None) -> dict:
    tok = create_admin_token(user or str(uuid.uuid4()), "admin", company or str(uuid.uuid4()))
    return {"Authorization": f"Bearer {tok}"}


def _device(dev=None) -> dict:
    tok = create_device_token(dev or str(uuid.uuid4()), str(uuid.uuid4()))
    return {"Authorization": f"Bearer {tok}"}


# Generous everywhere except the policy under test, so each test isolates one bucket.
def _cfg(**kw) -> RateLimitConfig:
    base = dict(burst_max=10_000, read_per_min=10_000, write_per_min=10_000,
                search_per_min=10_000, report_job_per_min=10_000,
                company_per_min=10_000, anon_per_min=10_000, device_per_min=10_000)
    base.update(kw)
    return RateLimitConfig(**base)


# ── policies ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_burst_limit_per_user():
    async with _client(_build(_cfg(burst_max=3))) as c:
        h = _admin()
        codes = [(await c.get("/v1/things", headers=h)).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        # A different user is unaffected (per-user bucket).
        assert (await c.get("/v1/things", headers=_admin())).status_code == 200


@pytest.mark.asyncio
async def test_read_and_write_are_separate_buckets():
    async with _client(_build(_cfg(read_per_min=2, write_per_min=2))) as c:
        h = _admin()
        assert [(await c.get("/v1/things", headers=h)).status_code for _ in range(3)] == [200, 200, 429]
        # reads exhausted, writes still have their own budget
        assert (await c.post("/v1/things", headers=h)).status_code == 200
        assert (await c.put("/v1/things", headers=h)).status_code == 200
        assert (await c.delete("/v1/things", headers=h)).status_code == 429


@pytest.mark.asyncio
async def test_head_counts_as_read():
    async with _client(_build(_cfg(read_per_min=1))) as c:
        h = _admin()
        assert (await c.head("/v1/things", headers=h)).status_code == 200
        assert (await c.get("/v1/things", headers=h)).status_code == 429


@pytest.mark.asyncio
async def test_search_bucket_only_for_nonempty_search_or_q():
    async with _client(_build(_cfg(search_per_min=2))) as c:
        h = _admin()
        # plain / empty-search reads never touch the search bucket
        for _ in range(4):
            assert (await c.get("/v1/things?search=", headers=h)).status_code == 200
            assert (await c.get("/v1/things", headers=h)).status_code == 200
        assert (await c.get("/v1/things?search=bob", headers=h)).status_code == 200
        assert (await c.get("/v1/things?q=bob", headers=h)).status_code == 200      # q= counts too
        r = await c.get("/v1/things?search=bob", headers=h)
        assert r.status_code == 429 and r.headers["x-ratelimit-policy"] == "search"
        # non-search reads still fine
        assert (await c.get("/v1/things", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_report_job_bucket():
    async with _client(_build(_cfg(report_job_per_min=2))) as c:
        h = _admin()
        assert (await c.post("/v1/reports/jobs", headers=h)).status_code == 200
        assert (await c.post("/v1/reports/jobs", headers=h)).status_code == 200
        r = await c.post("/v1/reports/jobs", headers=h)
        assert r.status_code == 429 and r.headers["x-ratelimit-policy"] == "report"
        assert (await c.post("/v1/things", headers=h)).status_code == 200   # other writes OK


@pytest.mark.asyncio
async def test_company_bucket_shared_by_users_and_not_burnt_by_rejected_requests():
    cid = str(uuid.uuid4())
    async with _client(_build(_cfg(company_per_min=4, burst_max=2))) as c:
        u1, u2 = _admin(company=cid), _admin(company=cid)
        # u1 hammers: 2 allowed (burst), then rejected by ITS OWN burst bucket —
        # rejected requests must not drain the company's shared budget.
        for _ in range(10):
            await c.get("/v1/things", headers=u1)
        # company counter only saw u1's 2 accepted requests -> u2 has 2 left
        assert (await c.get("/v1/things", headers=u2)).status_code == 200
        assert (await c.get("/v1/things", headers=u2)).status_code == 200
        r = await c.get("/v1/things", headers=_admin(company=cid))   # 5th company request
        assert r.status_code == 429 and r.headers["x-ratelimit-policy"] == "company"
        # another company unaffected
        assert (await c.get("/v1/things", headers=_admin())).status_code == 200


@pytest.mark.asyncio
async def test_anonymous_limited_per_ip_and_bad_token_falls_back_to_ip():
    async with _client(_build(_cfg(anon_per_min=2)), ip="9.9.9.9") as c:
        assert (await c.get("/v1/things")).status_code == 200
        # garbage token => treated as anonymous, same IP bucket
        assert (await c.get("/v1/things", headers={"Authorization": "Bearer nope"})).status_code == 200
        assert (await c.get("/v1/things")).status_code == 429
    async with _client(_build(_cfg(anon_per_min=2)), ip="9.9.9.8") as c2:
        # fresh app = fresh limiter; just prove a different IP key is independent
        assert (await c2.get("/v1/things")).status_code == 200


@pytest.mark.asyncio
async def test_identity_is_jwt_subject_not_ip_for_authenticated_users():
    app = _build(_cfg(burst_max=2, anon_per_min=1))
    h1, h2 = _admin(), _admin()
    async with _client(app, ip="7.7.7.7") as a, _client(app, ip="7.7.7.7") as b:
        # Two users behind ONE IP each get their own budget...
        assert (await a.get("/v1/things", headers=h1)).status_code == 200
        assert (await b.get("/v1/things", headers=h2)).status_code == 200
        # ...and the same user from a different IP shares one budget.
    async with _client(app, ip="8.8.8.8") as c:
        assert (await c.get("/v1/things", headers=h1)).status_code == 200
        assert (await c.get("/v1/things", headers=h1)).status_code == 429


def _expired(sub=None, ttype="admin", secret=None):
    import jwt
    from datetime import datetime, timedelta, timezone
    from app.core.config import get_settings
    s = get_settings()
    return jwt.encode({"sub": sub or str(uuid.uuid4()), "type": ttype, "company_id": "c",
                       "exp": datetime.now(timezone.utc) - timedelta(hours=1)},
                      secret or s.JWT_SECRET, algorithm=s.JWT_ALGORITHM)


@pytest.mark.asyncio
async def test_expired_token_is_keyed_by_subject_not_ip_and_never_trusted():
    """An office of lapsed sessions must not share (and exhaust) one IP bucket;
    an expired token still gets ANONYMOUS treatment (no user/company buckets)."""
    app = _build(_cfg(anon_per_min=2, burst_max=1))
    a, b = {"Authorization": f"Bearer {_expired()}"}, {"Authorization": f"Bearer {_expired()}"}
    async with _client(app, ip="6.6.6.6") as c:
        # each expired subject has its OWN anon budget of 2 even behind one IP...
        assert [(await c.get("/v1/things", headers=a)).status_code for _ in range(3)] == [200, 200, 429]
        assert [(await c.get("/v1/things", headers=b)).status_code for _ in range(2)] == [200, 200]
        # ...and the shared IP bucket is untouched, so a plain anonymous caller is fine.
        assert (await c.get("/v1/things")).status_code == 200
        # not treated as an authenticated user (burst_max=1 would have 429'd request #2)
        r = await c.get("/v1/things", headers=b)
        assert r.status_code == 429 and r.headers["x-ratelimit-policy"] == "anon"


@pytest.mark.asyncio
async def test_expired_token_with_bad_signature_falls_back_to_ip():
    forged = _expired(secret="not-the-real-secret-not-the-real-secret")
    async with _client(_build(_cfg(anon_per_min=1)), ip="6.6.6.7") as c:
        h = {"Authorization": f"Bearer {forged}"}
        assert (await c.get("/v1/things", headers=h)).status_code == 200
        assert (await c.get("/v1/things", headers=h)).status_code == 429       # IP bucket
        assert (await c.get("/v1/things")).status_code == 429                  # same IP bucket


@pytest.mark.asyncio
async def test_credential_endpoints_with_own_limiters_are_exempt_from_anon_bucket():
    async with _client(_build(_cfg(anon_per_min=1)), ip="4.4.4.4") as c:
        for path in ("/v1/auth/admin/login", "/v1/auth/register", "/v1/auth/enroll",
                     "/v1/setup/reset-admin"):
            assert {(await c.post(path)).status_code for _ in range(5)} == {200}, path
        # ...and they do not consume the anonymous bucket either
        assert (await c.get("/v1/things")).status_code == 200
        assert (await c.get("/v1/things")).status_code == 429


@pytest.mark.asyncio
async def test_google_oauth_uses_its_own_ip_bucket_not_the_anon_one():
    async with _client(_build(_cfg(anon_per_min=2)), ip="4.4.4.5") as c:
        # exhaust the plain anon bucket (e.g. expired-token noise from the office)
        for _ in range(3):
            await c.get("/v1/things")
        assert (await c.get("/v1/things")).status_code == 429
        # sign-in via Google still works, with its own (bounded) budget
        assert (await c.get("/v1/auth/google")).status_code == 200
        assert (await c.get("/v1/auth/google/callback")).status_code == 200
        r = await c.get("/v1/auth/google")
        assert r.status_code == 429 and r.headers["x-ratelimit-policy"] == "oauth"


@pytest.mark.asyncio
async def test_sse_query_token_is_keyed_by_user_not_ip():
    """EventSource can't send headers: /v1/devices/stream?token= must be keyed by
    the token's subject (and charge the user's buckets), not the shared IP."""
    app = _build(_cfg(burst_max=2, anon_per_min=1))
    t1 = _admin()["Authorization"].split()[1]
    t2 = _admin()["Authorization"].split()[1]
    async with _client(app, ip="2.2.2.2") as c:
        # anon limit is 1/min per IP, yet two users each stream fine
        assert (await c.get(f"/v1/devices/stream?token={t1}")).status_code == 200
        assert (await c.get(f"/v1/devices/stream?token={t2}")).status_code == 200
        # user-keyed burst bucket applies (burst_max=2)
        assert (await c.get(f"/v1/devices/stream?token={t1}")).status_code == 200
        r = await c.get(f"/v1/devices/stream?token={t1}")
        assert r.status_code == 429 and r.headers["x-ratelimit-policy"] == "burst"
        # a ?token= on a NON-stream path is ignored (stays anonymous by IP)
        assert (await c.get(f"/v1/things?token={t2}")).status_code == 200
        assert (await c.get(f"/v1/things?token={t2}")).status_code == 429


def test_default_policy_sizes():
    from app.core.config import Settings
    cfg = RateLimitConfig.from_settings(Settings())
    assert cfg.company_per_min == 6000       # ~100 RPS: tenant monopoly guard, not a user cap
    assert cfg.anon_per_min == 300           # an office behind one NAT


# ── device / agent traffic ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_bucket_per_token_not_per_ip():
    app = _build(_cfg(device_per_min=3, anon_per_min=1, burst_max=1))
    d1, d2 = _device(), _device()
    async with _client(app, ip="5.5.5.5") as c:        # many agents, ONE office NAT IP
        # per-IP anon limit (1) and user burst (1) must NOT apply to agents
        assert [(await c.post("/v1/ingest", headers=d1)).status_code for _ in range(4)] == [200, 200, 200, 429]
        assert (await c.post("/v1/ingest", headers=d2)).status_code == 200   # other device unaffected
        assert (await c.get("/v1/things", headers=d2)).status_code == 200    # any route is device-bucketed


@pytest.mark.asyncio
async def test_device_default_budget_allows_backlog_drain():
    """A drain posts full batches back-to-back: the default per-device budget must
    comfortably absorb a few hundred requests inside a minute."""
    cfg = RateLimitConfig()                      # production defaults
    async with _client(_build(cfg)) as c:
        h = _device()
        codes = [(await c.post("/v1/ingest", headers=h)).status_code for _ in range(300)]
        assert set(codes) == {200}


# ── exemptions ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exempt_paths_and_preflight_never_limited():
    async with _client(_build(_cfg(anon_per_min=1, burst_max=1)), ip="4.4.4.4") as c:
        for _ in range(5):
            assert (await c.get("/health")).status_code == 200
            assert (await c.get("/docs")).status_code == 200
            r = await c.options("/v1/things", headers={
                "Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
            assert r.status_code == 200
        # and the exempt traffic did not consume the anon budget
        assert (await c.get("/v1/things")).status_code == 200
        assert (await c.get("/v1/things")).status_code == 429


# ── 429 shape ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_429_has_retry_after_ratelimit_headers_and_cors():
    async with _client(_build(_cfg(burst_max=1))) as c:
        h = {**_admin(), "Origin": ORIGIN}
        ok = await c.get("/v1/things", headers=h)
        assert ok.status_code == 200
        assert ok.headers["x-ratelimit-limit"] == "1" and ok.headers["x-ratelimit-remaining"] == "0"
        r = await c.get("/v1/things", headers=h)
        assert r.status_code == 429
        assert r.json()["detail"]
        assert 1 <= int(r.headers["retry-after"]) <= 5
        assert r.headers["x-ratelimit-limit"] == "1"
        assert r.headers["x-ratelimit-remaining"] == "0"
        assert 1 <= int(r.headers["x-ratelimit-reset"]) <= 5
        # browser-readable: CORS headers survive on the 429
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert r.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_real_app_429_carries_cors_headers(monkeypatch):
    """Full create_app() stack: the 429 must flow through CORSMiddleware."""
    from app.core.config import get_settings
    from app.main import create_app
    s = get_settings()
    monkeypatch.setattr(s, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(s, "RATE_LIMIT_ANON_PER_MIN", 2)
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("3.3.3.3", 1)),
                                 base_url="http://t") as c:
        h = {"Origin": ORIGIN}
        # /v1/auth/me without credentials is rejected by HTTPBearer before any DB use
        for _ in range(2):
            assert (await c.get("/v1/auth/me", headers=h)).status_code in (401, 403)
        r = await c.get("/v1/auth/me", headers=h)
        assert r.status_code == 429
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert "retry-after" in r.headers
        # security headers (outermost middleware) are on it too
        assert r.headers["x-content-type-options"] == "nosniff"
        # /health stays reachable
        assert (await c.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_real_app_429_and_413_expose_retry_after_cross_origin(monkeypatch):
    """The dashboard is cross-origin: Retry-After must be listed in
    Access-Control-Expose-Headers on the limiter's 429 AND the body cap's 413."""
    from app.core.config import get_settings
    from app.main import create_app
    s = get_settings()
    monkeypatch.setattr(s, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(s, "RATE_LIMIT_ANON_PER_MIN", 1)
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("3.3.3.4", 1)),
                                 base_url="http://t") as c:
        h = {"Origin": ORIGIN}
        await c.get("/v1/auth/me", headers=h)
        r = await c.get("/v1/auth/me", headers=h)
        assert r.status_code == 429
        exposed = {x.strip().lower() for x in r.headers["access-control-expose-headers"].split(",")}
        assert {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                "x-ratelimit-reset", "x-ratelimit-policy"} <= exposed
        # 413 (oversized body) flows through CORS the same way. The login route is
        # exempt from the global limiter, so this reaches the body-size middleware.
        big = await c.post("/v1/auth/admin/login", headers=h, content=b"x" * (2 * 1024 * 1024))
        assert big.status_code == 413
        assert "retry-after" in {x.strip().lower()
                                 for x in big.headers["access-control-expose-headers"].split(",")}


# ── limiter backends ──────────────────────────────────────────────────────────

class FakeRedis:
    """Just enough redis for MultiWindowLimiter: register_script emulating the Lua."""
    def __init__(self, fail=False):
        self.store: dict[str, int] = {}
        self.fail = fail
        self.calls = 0

    def register_script(self, src):
        async def run(keys, args):
            self.calls += 1
            if self.fail:
                raise ConnectionError("redis down")
            n_gate, n = int(args[0]), len(keys)
            over, out = False, []
            for i, k in enumerate(keys, start=1):
                if i <= n_gate:
                    self.store[k] = self.store.get(k, 0) + 1
                    if self.store[k] > int(args[n + i]):
                        over = True
                    out.append(self.store[k])
                elif over:
                    out.append(self.store.get(k, 0))
                else:
                    self.store[k] = self.store.get(k, 0) + 1
                    out.append(self.store[k])
            return out
        return run


@pytest.mark.asyncio
async def test_limit_is_shared_across_workers_via_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(MultiWindowLimiter, "_client", staticmethod(lambda: fake))
    cfg = _cfg(burst_max=3)
    # two "workers": separate middleware + limiter instances, one Redis
    w1, w2 = _build(cfg, MultiWindowLimiter()), _build(cfg, MultiWindowLimiter())
    h = _admin()
    async with _client(w1) as a, _client(w2) as b:
        codes = [
            (await a.get("/v1/things", headers=h)).status_code,
            (await b.get("/v1/things", headers=h)).status_code,
            (await a.get("/v1/things", headers=h)).status_code,
            (await b.get("/v1/things", headers=h)).status_code,   # 4th overall -> 429
        ]
    assert codes == [200, 200, 200, 429]
    assert fake.calls == 4 and any(k.startswith("tt:rl:g:burst:") for k in fake.store)


@pytest.mark.asyncio
async def test_redis_down_falls_back_to_local_and_still_limits(monkeypatch):
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(MultiWindowLimiter, "_client", staticmethod(lambda: fake))
    lim = MultiWindowLimiter(redis_retry_secs=60)
    async with _client(_build(_cfg(burst_max=2), lim)) as c:
        h = _admin()
        codes = [(await c.get("/v1/things", headers=h)).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]              # NOT fail-open
    assert fake.calls == 3                             # breaker opens after 3 consecutive failures, then skipped


@pytest.mark.asyncio
async def test_redis_recovery_after_breaker_window(monkeypatch):
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(MultiWindowLimiter, "_client", staticmethod(lambda: fake))
    lim = MultiWindowLimiter(redis_retry_secs=0.05, redis_fail_threshold=1)
    b = [Bucket("x", "k", 100, 60)]
    await lim.consume(b)
    assert fake.calls == 1
    fake.fail = False
    await asyncio.sleep(0.08)
    res = await lim.consume(b)
    assert fake.calls == 2 and res[0].count == 1       # back on Redis


@pytest.mark.asyncio
async def test_single_pool_exhaustion_does_not_open_the_breaker(monkeypatch):
    """One 'Too many connections' / timeout under a burst must only fall back for
    THAT call; the next call goes straight back to Redis."""
    fake = FakeRedis()
    monkeypatch.setattr(MultiWindowLimiter, "_client", staticmethod(lambda: fake))
    lim = MultiWindowLimiter(redis_retry_secs=60)
    b = [Bucket("x", "k", 100, 60)]
    fake.fail = True
    await lim.consume(b)                      # transient failure -> local for this call
    fake.fail = False
    res = await lim.consume(b)                # Redis is used again immediately
    assert fake.calls == 2 and res[0].count == 1 and lim.redis_active
    # a success resets the streak: two more failures still don't trip it
    fake.fail = True
    await lim.consume(b); await lim.consume(b)
    assert lim.redis_active
    fake.fail = False
    await lim.consume(b)
    assert lim._fails == 0


@pytest.mark.asyncio
async def test_consecutive_failures_trip_then_one_failure_retrips_after_window(monkeypatch):
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(MultiWindowLimiter, "_client", staticmethod(lambda: fake))
    lim = MultiWindowLimiter(redis_retry_secs=0.05, redis_fail_threshold=3)
    b = [Bucket("x", "k", 100, 60)]
    for _ in range(3):
        await lim.consume(b)
    assert not lim.redis_active and fake.calls == 3
    await lim.consume(b)                       # breaker open: Redis skipped
    assert fake.calls == 3
    await asyncio.sleep(0.08)                  # half-open probe fails once -> re-trips at once
    await lim.consume(b)
    assert fake.calls == 4 and not lim.redis_active


@pytest.mark.asyncio
async def test_hung_redis_does_not_stall_requests(monkeypatch):
    class Hung:
        def register_script(self, src):
            async def run(keys, args):
                await asyncio.sleep(30)
            return run
    monkeypatch.setattr(MultiWindowLimiter, "_client", staticmethod(lambda: Hung()))
    lim = MultiWindowLimiter(redis_timeout=0.05, redis_retry_secs=60, redis_fail_threshold=1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    res = await lim.consume([Bucket("x", "k", 5, 60)])
    assert res[0].count == 1 and loop.time() - t0 < 1.0


@pytest.mark.asyncio
async def test_local_state_is_bounded():
    lim = MultiWindowLimiter(max_local_keys=50)
    for i in range(500):
        await lim.consume([Bucket("anon", f"ip:{i}", 5, 60)])
    assert len(lim._local) <= 50


@pytest.mark.asyncio
async def test_disabled_config_is_a_passthrough():
    async with _client(_build(_cfg(enabled=False, burst_max=1))) as c:
        h = _admin()
        assert {(await c.get("/v1/things", headers=h)).status_code for _ in range(5)} == {200}


# ── streaming ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sse_not_buffered_and_counted_once():
    """The first SSE chunk must reach the client while the handler is still
    running (no buffering), and a long-lived stream consumes ONE request."""
    release = asyncio.Event()

    async def sse_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: hello\n\n", "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    lim = MultiWindowLimiter()
    mw = RateLimitMiddleware(sse_app, config=_cfg(burst_max=5), limiter=lim)
    sent: list[dict] = []
    first_chunk = asyncio.Event()

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "http.response.body" and msg.get("body"):
            first_chunk.set()

    async def receive():
        await asyncio.sleep(3600)

    tok = _admin()["Authorization"].encode()
    scope = {"type": "http", "method": "GET", "path": "/v1/stream", "query_string": b"",
             "headers": [(b"authorization", tok)], "client": ("1.1.1.1", 1)}
    task = asyncio.create_task(mw(scope, receive, send))
    await asyncio.wait_for(first_chunk.wait(), 1.0)      # delivered BEFORE the app finished
    assert not task.done()
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert any(k == b"x-ratelimit-limit" for k, _ in start["headers"])
    release.set()
    await asyncio.wait_for(task, 1.0)
    # one connect = one hit on the burst bucket
    assert sum(v[0] for k, v in lim._local.items() if k.startswith("tt:rl:g:burst:")) == 1
