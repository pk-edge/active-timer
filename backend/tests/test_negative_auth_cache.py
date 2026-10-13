"""Negative auth caching (device + admin) and its invalidation."""
import time
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.AdminAuthCache import AdminAuthCache
from app.core.device_auth_cache import DeviceAuthCache


# ── pure cache units ──────────────────────────────────────────────────────────

def test_negative_hit_and_ttl():
    c = DeviceAuthCache(60, 100, negative_ttl_secs=0.05)
    assert not c.is_negative("t")
    c.set_negative("t", "dev-1")
    assert c.is_negative("t")
    time.sleep(0.06)
    assert not c.is_negative("t")
    assert c.stats()["negatives"] == 0


def test_negative_disabled_when_ttl_zero():
    c = DeviceAuthCache(60, 100, negative_ttl_secs=0)
    c.set_negative("t", "dev-1")
    assert not c.is_negative("t")


def test_enroll_restore_invalidation_drops_negative_entries():
    c = DeviceAuthCache(60, 100, negative_ttl_secs=60)
    c.set_negative("old-token", "dev-1")
    c.set_negative("other", "dev-2")
    c.invalidate_device("dev-1")                      # enroll / restore / archive call this
    assert not c.is_negative("old-token")
    assert c.is_negative("other")


def test_negative_set_refused_when_invalidation_raced():
    c = DeviceAuthCache(60, 100, negative_ttl_secs=60)
    seen = c.epoch()                                  # cold lookup starts (device inactive)
    c.invalidate_device("dev-1")                      # admin restores device mid-lookup
    c.set_negative("tok", "dev-1", seen_epoch=seen)   # stale "revoked" verdict must be dropped
    assert not c.is_negative("tok")


def test_negative_entries_bounded():
    c = DeviceAuthCache(60, 100, negative_ttl_secs=60, negative_max=5)
    for i in range(50):
        c.set_negative(f"t{i}", f"d{i}")
    assert c.stats()["negatives"] <= 5
    assert c.is_negative("t49")


def test_admin_negative_ttl_invalidate_and_disable():
    c = AdminAuthCache(ttl_secs=15, negative_ttl_secs=0.05)
    c.set_negative("tok")
    assert c.is_negative("tok")
    c.invalidate_all()                                # any user/role mutation
    assert not c.is_negative("tok")
    c.set_negative("tok")
    time.sleep(0.06)
    assert not c.is_negative("tok")
    off = AdminAuthCache(ttl_secs=0, negative_ttl_secs=15)   # test-suite mode: whole cache off
    off.set_negative("tok")
    assert not off.is_negative("tok")


def test_admin_negative_epoch_guard():
    c = AdminAuthCache(ttl_secs=15, negative_ttl_secs=15)
    seen = c.epoch()
    c.invalidate_all()                                # user activated mid-lookup
    c.set_negative("tok", seen_epoch=seen)
    assert not c.is_negative("tok")


# ── integration with the real dependencies (stub DB session, count queries) ───

class _Result:
    def __init__(self, row): self._row = row
    def first(self): return self._row
    def scalar_one_or_none(self): return self._row


class _FakeSession:
    def __init__(self, box): self.box = box
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, stmt):
        self.box["queries"] += 1
        return _Result(self.box["row"])


@pytest.fixture
def stub_db(monkeypatch):
    from app.api import deps
    from app.core.device_auth_cache import DeviceAuthCache as DAC
    box = {"queries": 0, "row": None}
    monkeypatch.setattr(deps, "AsyncSessionLocal", lambda: _FakeSession(box))
    fresh = DAC(60, 100, negative_ttl_secs=60)
    monkeypatch.setattr(deps, "device_auth_cache", fresh)
    # invalidate_device() in device_auth_cache.py talks to the module singleton
    from app.core import device_auth_cache as mod
    monkeypatch.setattr(mod, "device_auth_cache", fresh)
    return box, fresh


@pytest.mark.asyncio
async def test_unknown_device_token_costs_one_db_read_then_zero(stub_db):
    from app.api.deps import get_ingest_device_id
    from app.core.security import create_device_token
    box, cache = stub_db
    dev = str(uuid.uuid4())
    tok = create_device_token(dev, str(uuid.uuid4()))
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)

    for _ in range(5):                                # revoked agent retrying every 5 s
        with pytest.raises(HTTPException) as e:
            await get_ingest_device_id(creds)
        assert e.value.status_code == 401
    assert box["queries"] == 1                        # negative cache absorbed the other 4


@pytest.mark.asyncio
async def test_newly_enrolled_or_restored_device_is_not_locked_out(stub_db, monkeypatch):
    from app.api.deps import get_ingest_device_id
    from app.core import device_auth_cache as mod
    from app.core.security import create_device_token
    box, cache = stub_db
    dev = str(uuid.uuid4())
    tok = create_device_token(dev, str(uuid.uuid4()))
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)

    with pytest.raises(HTTPException):
        await get_ingest_device_id(creds)             # archived: negative cached
    assert cache.is_negative(tok)

    # admin restores / device re-enrolls -> endpoint calls invalidate_device()
    async def _noop(*a, **k): return None
    monkeypatch.setattr("app.core.RedisCache.cache_set", _noop)
    monkeypatch.setattr("app.core.RedisCache.cache_delete", _noop)
    await mod.invalidate_device(dev)
    assert not cache.is_negative(tok)

    box["row"] = (uuid.UUID(dev),)                    # DB now says: active
    assert await get_ingest_device_id(creds) == dev   # immediately accepted
    assert cache.get(tok) == dev                      # and positively cached


@pytest.mark.asyncio
async def test_admin_token_for_missing_user_is_negatively_cached(monkeypatch):
    from app.api import deps
    from app.core.security import create_admin_token
    cache = AdminAuthCache(ttl_secs=15, negative_ttl_secs=15)
    monkeypatch.setattr(deps, "admin_auth_cache", cache)
    calls = {"n": 0}

    async def fake_load(db, uid):
        calls["n"] += 1
        return None
    monkeypatch.setattr(deps, "_load_active_employee", fake_load)

    tok = create_admin_token(str(uuid.uuid4()), "admin", str(uuid.uuid4()))
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)
    for _ in range(4):
        with pytest.raises(HTTPException) as e:
            await deps.get_current_user(creds, db=None)
        assert e.value.status_code == 401
    assert calls["n"] == 1
    cache.invalidate_all()                            # e.g. user (re)activated
    with pytest.raises(HTTPException):
        await deps.get_current_user(creds, db=None)
    assert calls["n"] == 2                            # re-checked against the DB


@pytest.mark.asyncio
async def test_forged_token_never_reaches_negative_cache(stub_db):
    from app.api.deps import get_ingest_device_id
    box, cache = stub_db
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not.a.jwt")
    with pytest.raises(HTTPException):
        await get_ingest_device_id(creds)
    assert box["queries"] == 0 and cache.stats()["negatives"] == 0   # signature check comes first
