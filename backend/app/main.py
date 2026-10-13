import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.ingest_queue import ingest_queue
from app.db.base import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

settings = get_settings()

# Every *.onedge.co subdomain is permanently allowed (staging, prod, future).
# Shared by the CORS middleware AND the 500 handler so error responses keep
# their CORS headers — otherwise a backend crash surfaces in the browser as a
# misleading "No Access-Control-Allow-Origin" error instead of the real 500.
CORS_ORIGIN_REGEX = r"https://[a-zA-Z0-9\-]+\.onedge\.co"
_cors_re = re.compile(CORS_ORIGIN_REGEX)


def _cors_allow_origin(origin: str | None) -> str | None:
    """Return the origin to echo back if it is allowed, else None."""
    if not origin:
        return None
    if settings.CORS_ORIGINS == ["*"]:
        return origin
    if origin in settings.CORS_ORIGINS:
        return origin
    if _cors_re.fullmatch(origin):
        return origin
    return None


log = logging.getLogger(__name__)

# Known weak/placeholder secrets that must never reach a real deployment — a
# guessable JWT_SECRET lets anyone forge admin JWTs and OAuth state tokens.
_WEAK_SECRETS = {"", "change-me-in-production", "dev_jwt_secret_change_in_production"}


def _assert_secure_config() -> None:
    """Fail fast on a dangerous secret misconfiguration. Hard-stops a real
    deployment (ENVIRONMENT != development) booted with a default/dev JWT_SECRET;
    always warns so the weak secret is visible even in local development."""
    if settings.JWT_SECRET in _WEAK_SECRETS:
        if settings.ENVIRONMENT.lower() != "development":
            raise RuntimeError(
                f"Refusing to start in ENVIRONMENT={settings.ENVIRONMENT!r}: "
                "JWT_SECRET is a known default/dev value. A weak JWT_SECRET lets "
                "anyone forge admin JWTs and OAuth state. Set a strong, unique "
                "JWT_SECRET (e.g. `openssl rand -hex 32`)."
            )
        log.warning("JWT_SECRET is a known dev default — set a strong secret "
                    "before deploying (ENVIRONMENT=development, so allowed here).")
    if settings.CORS_ORIGINS == ["*"]:
        # Wildcard + credentials would let ANY site drive authenticated calls
        # from a victim's browser. Refuse outside local development.
        if settings.ENVIRONMENT.lower() != "development":
            raise RuntimeError(
                f"Refusing to start in ENVIRONMENT={settings.ENVIRONMENT!r}: "
                "CORS_ORIGINS='*' with credentialed CORS is unsafe. List the "
                "dashboard origins explicitly (comma-separated) instead."
            )
        log.warning("CORS_ORIGINS='*' — every origin is allowed. Development only.")


def _log_oauth_status() -> None:
    """One-line startup signal so a missing Google config is obvious in the logs
    at deploy time, instead of surfacing only when a user clicks 'Sign in'."""
    from app.core.secrets import load_google_credentials
    creds = load_google_credentials()
    if creds.get("client_id") and creds.get("client_secret"):
        log.info("Google OAuth: configured")
    else:
        log.warning(
            "Google OAuth: NOT configured — /v1/auth/google will return 503. Set "
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (or SECRETS_KEY + "
            "secrets/google_credentials.enc) in this backend's environment."
        )


def make_singleton_hooks(singletons):
    """-> (start_all, stop_all) for the leader-elected background loops.

    `singletons` is an ordered sequence of (name, module) whose start()/stop()
    are idempotent. stop_all isolates every stop() (one failure never skips the
    others, and stop() tolerates never-started). start_all stops whatever DID
    start if a later start fails, so the elector's retry begins from a clean
    slate instead of leaving half-started loops, then re-raises so the worker
    is not marked leader.
    """
    async def stop_all() -> None:
        for name, mod in reversed(tuple(singletons)):
            try:
                await mod.stop()
            except Exception:  # noqa: BLE001
                log.exception("singleton %s: stop failed", name)

    async def start_all() -> None:
        try:
            for _name, mod in singletons:
                await mod.start()
        except Exception:
            await stop_all()
            raise

    return start_all, stop_all


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core import broadcaster, audit, RedisClient
    _assert_secure_config()          # refuse to boot a real deployment with a weak secret
    _log_oauth_status()              # surface missing Google OAuth config at startup
    await init_db()                  # migrations + RBAC seed + bootstrap admin (all idempotent)
    await RedisClient.startup()      # open Redis cache client (separate pool from pub/sub)
    await broadcaster.startup()      # open Redis pub/sub + listener BEFORE workers publish
    from app.core.AuthInvalidationBus import auth_bus
    await auth_bus.start()           # cross-worker auth-cache invalidation (Redis pub/sub)
    await audit.start()              # background audit writer — keeps audit off the request path
    await ingest_queue.start()       # start 30-second COPY flusher (PER-WORKER: drains this process's buffer)
    # Share live device state with sibling uvicorn workers (per-worker device_cache
    # would otherwise hide devices that post to another worker).
    from app.core.DeviceStateSync import DeviceStateSync
    state_sync = DeviceStateSync(ingest_queue.device_cache)
    ingest_queue.on_state_update = state_sync.note
    await state_sync.start()

    # Singleton loops operate on SHARED DB state, so with N workers they must run
    # in exactly one: gated by a leader election (host file lock + Redis lock,
    # see app/core/LeaderLock.py). Gaining leadership starts them, losing it stops them.
    from app.core import report_cache, tech_cache, device_janitor
    from app.core.LeaderLock import LeaderElector

    _start_singletons, _stop_singletons = make_singleton_hooks((
        ("report_cache", report_cache),      # pre-compute report aggregates
        ("tech_cache", tech_cache),          # pre-compute tech utilization aggregates
        ("device_janitor", device_janitor),  # auto-archive devices inactive beyond threshold
    ))

    elector = LeaderElector(
        "background", _start_singletons, _stop_singletons,
        ttl_secs=settings.LEADER_LOCK_TTL_SECS,
        interval_secs=settings.LEADER_LOCK_INTERVAL_SECS,
    )
    await elector.start()
    yield
    await elector.stop()             # stops singleton loops (if leader) + releases both locks
    await state_sync.stop()
    ingest_queue.on_state_update = None
    await ingest_queue.stop()        # final COPY flush + clean exit
    await audit.stop()               # flush queued audit rows, then stop the writer
    await auth_bus.stop()
    await broadcaster.shutdown()     # stop SSE pub/sub listener + close that Redis client
    await RedisClient.shutdown()     # close cache Redis client last
    from app.db.base import background_engine, engine, ingest_engine
    await background_engine.dispose()
    await ingest_engine.dispose()
    await engine.dispose()


def create_app() -> FastAPI:
    # API docs off outside development unless explicitly re-enabled — the
    # schema enumerates every route/parameter for an attacker for free.
    docs_enabled = settings.API_DOCS_ENABLED
    if docs_enabled is None:
        docs_enabled = settings.ENVIRONMENT.lower() == "development"

    app = FastAPI(
        title="TimeTracker API",
        version="1.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
        # orjson for every response: serializes large list payloads in fast C so
        # the single async worker's event loop is not blocked on stdlib json while
        # other requests wait (the load-induced "request failed / CORS" symptom).
        default_response_class=ORJSONResponse,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    # Cancel abandoned GET requests: when a dashboard tab navigates away, the
    # in-flight query is cancelled instead of finishing for nobody — this keeps
    # the small HTTP pool light under click-around navigation. Added FIRST so
    # it wraps innermost (runs closest to the route handlers).
    from app.core.request_cancel import CancelOnDisconnectMiddleware
    app.add_middleware(
        CancelOnDisconnectMiddleware,
        skip_paths=("/v1/home/stream",),   # SSE manages its own disconnects
    )

    # Request-body caps (413) and global rate limiting (429). Both are pure ASGI
    # (SSE is never buffered) and added BEFORE CORS so they sit INSIDE it: their
    # 413/429 responses flow through CORSMiddleware and carry the CORS headers —
    # without them the dashboard can't read the error. Order (outer -> inner):
    # rate limit -> body size -> cancel-on-disconnect -> routes.
    from app.core.RequestSizeLimit import RequestSizeLimitMiddleware, rules_from_settings
    if settings.MAX_REQUEST_BODY_BYTES > 0:
        app.add_middleware(
            RequestSizeLimitMiddleware,
            default_max=settings.MAX_REQUEST_BODY_BYTES,
            rules=rules_from_settings(settings),
        )
    from app.core.RateLimitMiddleware import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

    # allow_origin_regex permanently covers every *.onedge.co subdomain so no
    # env var is needed in production.  CORS_ORIGINS still works for local dev
    # and any extra origins passed via env.
    wildcard = settings.CORS_ORIGINS == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[] if wildcard else settings.CORS_ORIGINS,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_credentials=True,
        # Scoped to what the dashboard/agent actually send — a wildcard here
        # needlessly pre-authorizes arbitrary methods/headers for allowed origins.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Setup-Secret"],
        # Cross-origin JS can only read CORS-safelisted response headers unless
        # they are exposed — without this the dashboard cannot read Retry-After
        # on a 429 (the rate limiter sits INSIDE CORS, so its 429/413 pass
        # through this middleware and pick the header up).
        expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining",
                        "X-RateLimit-Reset", "X-RateLimit-Policy"],
    )

    # Compress large JSON payloads on the wire (device/user/log/report lists are
    # often 5-20× smaller gzipped). Added AFTER CORS so it sits OUTERMOST and
    # compresses the final bytes with CORS headers intact. SSE-safe: the
    # realtime push stream (text/event-stream) is never buffered/compressed.
    from app.core.gzip_safe import SSESafeGZipMiddleware
    app.add_middleware(SSESafeGZipMiddleware, minimum_size=600, compresslevel=5)

    # ── Security headers ──────────────────────────────────────────────────────
    # Pure-ASGI (not BaseHTTPMiddleware) so SSE streaming is never buffered.
    class _SecurityHeaders:
        def __init__(self, app):  # noqa: D401
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = message.setdefault("headers", [])
                    headers.extend([
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                    ])
                    if scope.get("scheme") == "https":
                        headers.append((
                            b"strict-transport-security",
                            b"max-age=63072000; includeSubDomains",
                        ))
                await send(message)

            await self.app(scope, receive, send_wrapper)

    app.add_middleware(_SecurityHeaders)

    # ── Global exception handler ──────────────────────────────────────────────
    # A 500 is generated by Starlette's ServerErrorMiddleware, which runs OUTSIDE
    # the CORS middleware — so we must attach CORS headers here ourselves or the
    # browser reports the crash as a CORS error and hides the real cause.
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logging.getLogger("main").exception("Unhandled error on %s %s", request.method, request.url)
        resp = JSONResponse(status_code=500, content={"detail": "Internal server error"})
        allow = _cors_allow_origin(request.headers.get("origin"))
        if allow:
            resp.headers["Access-Control-Allow-Origin"] = allow
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Vary"] = "Origin"
        return resp

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(api_router, prefix="/v1")

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    @app.get("/health/db", tags=["health"])
    async def health_db():
        """Liveness for the DB layer — surfaces connectivity errors directly so a
        login 500 can be confirmed/ruled-out as a database problem."""
        from sqlalchemy import text
        from app.db.base import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(text("SELECT 1"))
            return {"status": "ok", "db": "reachable"}
        except Exception as e:
            logging.getLogger("main").exception("DB health check failed")
            return JSONResponse(status_code=503, content={"status": "error", "db": str(e)})

    return app


app = create_app()
