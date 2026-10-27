"""pytest bootstrap for backend RBAC/scoping tests.

Adds the backend root to sys.path so `import app...` resolves when running
`pytest` from the container's /app working directory.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Disable the in-process admin-auth cache: tests seed/mutate employees and
# roles with raw SQL in a never-committed session, which no invalidation hook
# can observe — a cached auth result would leak across those mutations.
# Must be set BEFORE the first get_settings() call (i.e. before app imports).
os.environ.setdefault("ADMIN_AUTH_CACHE_TTL_SECS", "0")
# The suite fires hundreds of requests from one test client/identity; the global
# API rate limiter (production default: ON) would 429 them. Tests that exercise
# the limiter construct RateLimitMiddleware with an explicit config instead.
os.environ["RATE_LIMIT_ENABLED"] = "false"   # unconditional: compose passes RATE_LIMIT_ENABLED=true

pytest_plugins = ("pytest_asyncio",)

import asyncio

import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def event_loop():
    """One event loop for the whole session. The app's async engine pools asyncpg
    connections bound to the loop that created them; with the default function-scoped
    loop, a second DB-touching test would reuse a connection from a closed loop and
    fail ("Future attached to a different loop"). A session loop keeps them valid."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    """A live-Postgres session that is never committed — it rolls back on exit so
    fixtures created in it never pollute real data (same approach as test_scoping).
    Shared with the ASGI app via the `client` fixture below."""
    from app.db.base import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session):
    """httpx AsyncClient over the ASGI app, with `get_db` overridden to hand the
    endpoint the SAME rolled-back session as the test, so endpoint tests stay
    isolated. httpx ASGITransport does not run the lifespan, so init_db/migrations
    don't fire — the live DB already has the schema."""
    import httpx

    from app.api.deps import get_db
    from app.main import create_app

    app = create_app()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
