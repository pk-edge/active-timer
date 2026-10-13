"""Every engine pins the DB session timezone to UTC (report day-bucketing and
CURRENT_DATE assume UTC) — live Postgres, like the rest of the suite."""
import pytest
from sqlalchemy import text

from app.db import base


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["engine", "ingest_engine", "background_engine"])
async def test_session_timezone_is_utc_on_every_engine(name):
    eng = getattr(base, name)
    async with eng.connect() as conn:
        assert (await conn.execute(text("SHOW timezone"))).scalar() == "UTC"


def test_timezone_is_merged_with_statement_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(base, "create_async_engine", lambda url, **kw: seen.append(kw) or object())
    base._make_engine(1, 0, statement_timeout_ms=1234)
    base._make_engine(1, 0)
    assert seen[0]["connect_args"]["server_settings"] == {"timezone": "UTC", "statement_timeout": "1234"}
    assert seen[1]["connect_args"]["server_settings"] == {"timezone": "UTC"}
