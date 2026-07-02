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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core import broadcaster, audit, RedisClient
    _assert_secure_config()          # refuse to boot a real deployment with a weak secret
    _log_oauth_status()              # surface missing Google OAuth config at startup
    await init_db()                  # migrations + RBAC seed + bootstrap admin (all idempotent)
    await RedisClient.startup()      # open Redis cache client (separate pool from pub/sub)
    await broadcaster.startup()      # open Redis pub/sub + listener BEFORE workers publish
    await audit.start()              # background audit writer — keeps audit off the request path
    await ingest_queue.start()       # start 30-second COPY flusher
    from app.core import report_cache
    await report_cache.start()       # pre-compute report aggregates every 30 min
    from app.core import tech_cache
    await tech_cache.start()         # pre-compute tech utilization aggregates every 30 min
    from app.core import device_janitor
    await device_janitor.start()     # auto-archive devices inactive beyond company threshold
    yield
    await device_janitor.stop()      # stop janitor
    await tech_cache.stop()          # stop tech cache worker
    await report_cache.stop()        # stop report cache worker
    await ingest_queue.stop()        # final COPY flush + clean exit
    await audit.stop()               # flush queued audit rows, then stop the writer
    await broadcaster.shutdown()     # stop SSE pub/sub listener + close that Redis client
    await RedisClient.shutdown()     # close cache Redis client last
    from app.db.base import engine, ingest_engine
    await ingest_engine.dispose()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="TimeTracker API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
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
