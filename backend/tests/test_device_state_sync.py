"""Cross-worker device-state sharing + migration advisory lock."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

from app.core import DeviceStateSync as DS
from app.core.DeviceStateSync import DeviceStateSync


class FakeHashRedis:
    """Emulates the two Lua scripts (CAS push / compare-and-HDEL prune) + HGETALL."""
    def __init__(self):
        self.h: dict[str, str] = {}
        self.eval_calls = 0

    async def eval(self, script, numkeys, key, *args):
        import json as _json
        self.eval_calls += 1
        if script == DS._CAS_LUA:
            written = 0
            for i in range(1, len(args), 3):
                did, val, ls = args[i], args[i + 1], float(args[i + 2])
                cur = self.h.get(did)
                if cur is not None:
                    try:
                        if float(_json.loads(cur)["ls"]) > ls:
                            continue
                    except Exception:
                        pass
                self.h[did] = val
                written += 1
            return written
        if script == DS._HDEL_IF_SAME_LUA:
            n = 0
            for i in range(0, len(args), 2):
                if self.h.get(args[i]) == args[i + 1]:
                    del self.h[args[i]]
                    n += 1
            return n
        raise AssertionError("unexpected script")

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.h.items()}


def _state(app="Code", t=None):
    return {"last_seen": datetime.fromtimestamp(t or time.time(), tz=timezone.utc),
            "latest_type": "window_change", "current_app": app, "updated_at": time.time()}


@pytest.mark.asyncio
async def test_state_posted_to_one_worker_becomes_visible_on_the_other():
    r = FakeHashRedis()
    cache_a, cache_b = {}, {}
    a = DeviceStateSync(cache_a, redis_getter=lambda: r)
    b = DeviceStateSync(cache_b, redis_getter=lambda: r)
    st = _state("Figma")
    cache_a["dev-1"] = st
    a.note("dev-1", st)
    await a.push()
    assert await b.pull() == 1
    got = cache_b["dev-1"]
    assert got["current_app"] == "Figma" and got["latest_type"] == "window_change"
    assert got["last_seen"].timestamp() == pytest.approx(st["last_seen"].timestamp())
    # idempotent: nothing new -> nothing merged
    assert await b.pull() == 0


@pytest.mark.asyncio
async def test_older_remote_state_never_overwrites_newer_local():
    r = FakeHashRedis()
    cache_a, cache_b = {}, {}
    a = DeviceStateSync(cache_a, redis_getter=lambda: r)
    b = DeviceStateSync(cache_b, redis_getter=lambda: r)
    old = _state("Old", t=time.time() - 30)
    a.note("dev-1", old); await a.push()
    cache_b["dev-1"] = _state("New")
    await b.pull()
    assert cache_b["dev-1"]["current_app"] == "New"


@pytest.mark.asyncio
async def test_no_redis_is_a_silent_noop_and_failed_push_is_retried():
    a = DeviceStateSync({}, redis_getter=lambda: None)
    a.note("d", _state())
    assert await a.push() == 0 and await a.pull() == 0

    class Boom(FakeHashRedis):
        async def eval(self, *a, **k):
            raise ConnectionError("x")
    b = DeviceStateSync({}, redis_getter=lambda: Boom())
    b.note("d", _state())
    assert await b.push() == 0
    assert "d" in b._pending                          # kept for the next cycle


@pytest.mark.asyncio
async def test_stale_entries_are_pruned_from_the_hash():
    r = FakeHashRedis()
    a = DeviceStateSync({}, redis_getter=lambda: r)
    old = _state(t=time.time() - 7200); old["updated_at"] = time.time() - 7200
    a.note("gone", old); await a.push()
    b = DeviceStateSync({}, redis_getter=lambda: r)
    await b.pull()
    assert "gone" not in r.h


@pytest.mark.asyncio
async def test_push_never_overwrites_a_newer_state_with_an_older_one():
    """Worker A flushes a stale buffered state AFTER worker B already wrote a
    newer one: the compare-and-set must keep B's."""
    r = FakeHashRedis()
    a = DeviceStateSync({}, redis_getter=lambda: r)
    b = DeviceStateSync({}, redis_getter=lambda: r)
    now = time.time()
    b.note("dev-1", _state("Newer", t=now)); await b.push()
    a.note("dev-1", _state("Older", t=now - 20)); await a.push()
    import json
    assert json.loads(r.h["dev-1"])["ca"] == "Newer"
    # a genuinely newer state still wins
    a.note("dev-1", _state("Newest", t=now + 5)); await a.push()
    assert json.loads(r.h["dev-1"])["ca"] == "Newest"


@pytest.mark.asyncio
async def test_push_chunks_large_batches_and_requeues_unsent_on_failure(monkeypatch):
    monkeypatch.setattr(DS, "_CHUNK", 2)
    r = FakeHashRedis()
    a = DeviceStateSync({}, redis_getter=lambda: r)
    for i in range(5):
        a.note(f"d{i}", _state(f"A{i}"))
    assert await a.push() == 5 and r.eval_calls == 3 and len(r.h) == 5

    holder = {}

    class FailSecond(FakeHashRedis):
        async def eval(self, *a, **k):
            if self.eval_calls >= 1:
                raise ConnectionError("x")
            # a newer note() lands while the first chunk is in flight
            holder["b"].note("d3", _state("Fresh", t=time.time() + 100))
            return await super().eval(*a, **k)
    r2 = FailSecond()
    b = DeviceStateSync({}, redis_getter=lambda: r2)
    holder["b"] = b
    for i in range(5):
        b.note(f"d{i}", _state(f"B{i}"))
    assert await b.push() == 2                          # first chunk confirmed
    assert set(b._pending) == {"d2", "d3", "d4"}        # rest re-queued
    assert "Fresh" in b._pending["d3"]                  # newer note() not clobbered by the re-queue


@pytest.mark.asyncio
async def test_prune_only_deletes_if_value_is_still_the_stale_one():
    """A fresh state written between our HGETALL and the prune must survive."""
    r = FakeHashRedis()
    old = _state(t=time.time() - 7200); old["updated_at"] = time.time() - 7200
    a = DeviceStateSync({}, redis_getter=lambda: r)
    a.note("dev-1", old); await a.push()

    class Racy(FakeHashRedis):
        """HGETALL returns the stale snapshot, then the device posts fresh state
        before the prune runs."""
        def __init__(self, base):
            super().__init__(); self.h = base.h
        async def hgetall(self, key):
            snap = await super().hgetall(key)
            fresh = DeviceStateSync({}, redis_getter=lambda: self)
            fresh.note("dev-1", _state("Fresh"))
            await fresh.push()
            return snap
    racy = Racy(r)
    b = DeviceStateSync({}, redis_getter=lambda: racy)
    await b.pull()
    import json
    assert "dev-1" in racy.h and json.loads(racy.h["dev-1"])["ca"] == "Fresh"


# ── migration advisory lock (live Postgres, like the rest of the suite) ───────

@pytest.mark.asyncio
async def test_migrations_are_serialized_across_processes(monkeypatch):
    from app.db import base

    active, peak, runs = 0, 0, 0

    async def fake_locked():
        nonlocal active, peak, runs
        active += 1
        peak = max(peak, active)
        runs += 1
        await asyncio.sleep(0.3)
        active -= 1

    monkeypatch.setattr(base, "_migrate_and_seed_locked", fake_locked)
    monkeypatch.setattr(base, "_MIGRATION_LOCK_POLL_SECS", 0.05)
    await asyncio.gather(base._migrate_and_seed(), base._migrate_and_seed())
    assert runs == 2 and peak == 1                    # never concurrent

    # lock is released afterwards: a third run acquires instantly
    await asyncio.wait_for(base._migrate_and_seed(), 3)
