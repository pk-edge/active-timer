"""Security regression: POST /v1/setup/reset-admin is opt-in and secret-gated.

Once an admin exists (locked mode), the endpoint must reject any request that
isn't authorized by an explicit SETUP_SECRET — and must NOT accept JWT_SECRET as
a fallback (the over-broad behavior we removed). When SETUP_SECRET is unset the
endpoint is effectively disabled.

Uses the httpx/ASGI `client` + shared `db_session` fixtures from conftest. All
three cases reject BEFORE the endpoint's commit, so nothing is persisted; the
session rolls back on exit.
"""
import uuid

import pytest

from app.core.config import get_settings
from app.db.admin import ensure_admin


@pytest.mark.asyncio
async def test_reset_admin_is_opt_in_and_secret_gated(client, db_session, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    email = f"admin-{tag}@x.io"

    # LOCKED mode: at least one admin must exist (visible to the endpoint via the
    # shared session). flush() (no commit) keeps it out of the real DB.
    await ensure_admin(
        db_session, company_name=f"SetupTest-{tag}", email=email,
        name="Admin", password="StrongPass123!",
    )
    await db_session.flush()

    body = {"company": f"SetupTest-{tag}", "email": email, "name": "Admin", "password": "NewPass123!"}
    settings = get_settings()
    path = "/v1/setup/reset-admin"

    # 1. SETUP_SECRET unset → endpoint disabled → reject even with a header present.
    monkeypatch.setattr(settings, "SETUP_SECRET", "")
    r = await client.post(path, json=body, headers={"X-Setup-Secret": "anything"})
    assert r.status_code == 401, r.text

    # 2. SETUP_SECRET set, wrong value → reject.
    monkeypatch.setattr(settings, "SETUP_SECRET", "the-real-secret")
    r = await client.post(path, json=body, headers={"X-Setup-Secret": "wrong"})
    assert r.status_code == 401, r.text

    # 3. JWT_SECRET must NOT be accepted as a fallback (the bug we removed).
    r = await client.post(path, json=body, headers={"X-Setup-Secret": settings.JWT_SECRET})
    assert r.status_code == 401, r.text
