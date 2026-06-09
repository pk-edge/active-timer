import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.ingest_queue import ingest_queue
from app.db.base import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

settings = get_settings()


async def _auto_seed() -> None:
    """Create company + first admin from env vars if they don't exist yet.

    Triggered by setting all four SEED_* variables in .env or docker-compose.
    Safe to leave enabled — skips silently if the company already exists.
    """
    s = settings
    if not (s.SEED_COMPANY and s.SEED_EMAIL and s.SEED_PASSWORD):
        return

    import secrets
    from sqlalchemy import select
    from app.db.base import AsyncSessionLocal
    from app.db.models import Company, Employee
    from app.core.security import hash_password

    log = logging.getLogger("auto_seed")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Company).where(Company.name == s.SEED_COMPANY))
        company = result.scalar_one_or_none()
        if company:
            log.info("Auto-seed skipped — company '%s' already exists", s.SEED_COMPANY)
            return

        company = Company(name=s.SEED_COMPANY, enrollment_token=secrets.token_hex(24))
        db.add(company)
        await db.flush()

        emp = Employee(
            company_id=company.id,
            email=s.SEED_EMAIL.lower(),
            name=s.SEED_NAME,
            role="admin",
            password_hash=hash_password(s.SEED_PASSWORD),
        )
        db.add(emp)
        await db.commit()

        log.info("Auto-seed complete — company '%s', admin '%s'", s.SEED_COMPANY, s.SEED_EMAIL)
        log.info("Enrollment token: %s", company.enrollment_token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _auto_seed()           # no-op if SEED_* vars are not set
    await ingest_queue.start()   # start async worker pool
    yield
    await ingest_queue.stop()    # drain + shutdown workers cleanly


def create_app() -> FastAPI:
    app = FastAPI(
        title="TimeTracker API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    # When origins is ["*"] credentials must be False (browser spec).
    # When a specific origin list is set, credentials are allowed.
    wildcard = settings.CORS_ORIGINS == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=not wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Global exception handler ──────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logging.getLogger("main").exception("Unhandled error on %s %s", request.method, request.url)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(api_router, prefix="/v1")

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
