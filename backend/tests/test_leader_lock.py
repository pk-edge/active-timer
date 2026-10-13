"""Leader election: exactly one holder, renewal, token-checked release, takeover,
Redis-down behaviour, callback transitions."""
import asyncio
import time

import pytest

from app.core import LeaderLock
from app.core.LeaderLock import LeaderElector, _RELEASE_LUA, _RENEW_LUA


class Clock:
    def __init__(self):
        self.t = 1000.0


class FakeRedis:
    """SET NX PX + token-checked eval, with an injectable clock for expiry."""
    def __init__(self, clock: Clock):
        self.clock = clock
        self.data: dict[str, tuple[str, float]] = {}
        self.down = False

    def _live(self, key):
        v = self.data.get(key)
        if v and v[1] <= self.clock.t:
            del self.data[key]
            v = None
        return v

    async def set(self, key, value, nx=False, px=None):
        if self.down:
            raise ConnectionError("down")
        if nx and self._live(key):
            return None
        self.data[key] = (value, self.clock.t + px / 1000)
        return True

    async def eval(self, script, numkeys, key, token, *rest):
        if self.down:
            raise ConnectionError("down")
        cur = self._live(key)
        if not cur or cur[0] != token:
            return 0
        if script == _RENEW_LUA:
            self.data[key] = (token, self.clock.t + int(rest[0]) / 1000)
            return 1
        if script == _RELEASE_LUA:
            del self.data[key]
            return 1
        raise AssertionError("unexpected script")


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    return c


def _elector(tmp_path, redis, name="bg", lockfile="a.lock", events=None, **kw):
    events = events if events is not None else []

    async def gain():
        events.append(("gain", lockfile))

    async def lose():
        events.append(("lose", lockfile))

    e = LeaderElector(name, gain, lose, ttl_secs=30, interval_secs=10,
                      lock_path=str(tmp_path / lockfile),
                      redis_getter=(lambda: redis), **kw)
    return e, events


@pytest.mark.asyncio
async def test_only_one_leader_across_replicas_via_redis(tmp_path, clock):
    r = FakeRedis(clock)
    a, ev_a = _elector(tmp_path, r, lockfile="replicaA.lock")
    b, ev_b = _elector(tmp_path, r, lockfile="replicaB.lock")      # different host
    assert await a.tick() is True
    assert await b.tick() is False
    assert ev_a == [("gain", "replicaA.lock")] and ev_b == []
    assert sum([a.is_leader, b.is_leader]) == 1
    await a.stop(); await b.stop()


@pytest.mark.asyncio
async def test_only_one_leader_per_host_via_file_lock_even_without_redis(tmp_path):
    a, _ = _elector(tmp_path, None, lockfile="same.lock")
    b, _ = _elector(tmp_path, None, lockfile="same.lock")          # sibling worker, same host
    assert await a.tick() is True
    assert await b.tick() is False
    await a.stop()
    assert await b.tick() is True                                   # released -> sibling takes over
    await b.stop()


@pytest.mark.asyncio
async def test_two_workers_same_host_with_redis_exactly_one(tmp_path, clock):
    r = FakeRedis(clock)
    a, _ = _elector(tmp_path, r, lockfile="h.lock")
    b, _ = _elector(tmp_path, r, lockfile="h.lock")
    res = [await a.tick(), await b.tick()]
    assert res.count(True) == 1
    await a.stop(); await b.stop()


@pytest.mark.asyncio
async def test_renewal_extends_ttl_and_is_token_checked(tmp_path, clock):
    r = FakeRedis(clock)
    a, _ = _elector(tmp_path, r)
    assert await a.tick()
    key = "tt:leader:bg"
    first_expiry = r.data[key][1]
    clock.t += 10
    assert await a.tick()                                           # renew
    assert r.data[key][1] == pytest.approx(clock.t + 30)
    assert r.data[key][1] > first_expiry
    # someone else now owns the key: our renew must NOT extend it
    r.data[key] = ("other-token", clock.t + 5)
    clock.t += 1
    assert await a.tick() is False                                  # lost, cannot retake (held)
    assert r.data[key] == ("other-token", pytest.approx(clock.t + 4))
    await a.stop()


@pytest.mark.asyncio
async def test_takeover_after_expiry(tmp_path, clock):
    r = FakeRedis(clock)
    a, ev_a = _elector(tmp_path, r, lockfile="A.lock")
    b, ev_b = _elector(tmp_path, r, lockfile="B.lock")
    assert await a.tick() is True
    assert await b.tick() is False
    # leader A is SIGKILLed: stops renewing, its Redis key expires after TTL
    clock.t += 31
    assert await b.tick() is True
    assert ev_b == [("gain", "B.lock")]
    a._file.release()                                               # (dead process)


@pytest.mark.asyncio
async def test_graceful_release_allows_immediate_takeover_and_never_deletes_foreign_lock(tmp_path, clock):
    r = FakeRedis(clock)
    a, ev_a = _elector(tmp_path, r, lockfile="A.lock")
    b, _ = _elector(tmp_path, r, lockfile="B.lock")
    await a.tick(); assert await b.tick() is False
    await a.stop()
    assert ("lose", "A.lock") in ev_a and "tt:leader:bg" not in r.data
    assert await b.tick() is True                                   # no waiting for TTL
    # B's key must survive a stale former holder's release attempt
    stale, _ = _elector(tmp_path, r, lockfile="C.lock")
    stale._holds_redis = True
    await stale.stop()
    assert r.data["tt:leader:bg"][0] == b._token
    await b.stop()


@pytest.mark.asyncio
async def test_redis_outage_keeps_host_leader_running_not_doubling(tmp_path, clock):
    r = FakeRedis(clock)
    a, ev_a = _elector(tmp_path, r, lockfile="h.lock")
    b, ev_b = _elector(tmp_path, r, lockfile="h.lock")              # sibling worker
    await a.tick(); await b.tick()
    r.down = True
    assert await a.tick() is True                                   # leader keeps leading
    assert await b.tick() is False                                  # sibling still blocked by flock
    assert ev_a == [("gain", "h.lock")] and ev_b == []              # no flapping, no double work
    r.down = False
    assert await a.tick() is True                                   # renews (key still valid)
    await a.stop(); await b.stop()


@pytest.mark.asyncio
async def test_transient_expiry_retaken_without_flapping(tmp_path, clock):
    r = FakeRedis(clock)
    a, ev = _elector(tmp_path, r)
    await a.tick()
    clock.t += 100                                                  # key expired while Redis was away
    assert await a.tick() is True
    assert ev == [("gain", "a.lock")]                               # no lose/gain cycle


@pytest.mark.asyncio
async def test_on_gain_failure_is_retried_and_not_marked_leader(tmp_path, clock):
    r = FakeRedis(clock)
    calls = {"n": 0}

    async def gain():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    async def lose():
        pass

    e = LeaderElector("bg", gain, lose, ttl_secs=30, interval_secs=10,
                      lock_path=str(tmp_path / "x.lock"), redis_getter=lambda: r)
    assert await e.tick() is False and not e.is_leader
    assert await e.tick() is True and calls["n"] == 2
    await e.stop()


@pytest.mark.asyncio
async def test_start_stop_lifecycle_runs_loop(tmp_path, clock):
    r = FakeRedis(clock)
    events = []

    async def gain(): events.append("gain")
    async def lose(): events.append("lose")

    e = LeaderElector("bg", gain, lose, ttl_secs=30, interval_secs=0.01,
                      lock_path=str(tmp_path / "l.lock"), redis_getter=lambda: r)
    await e.start()
    assert e.is_leader and events == ["gain"]
    await asyncio.sleep(0.05)                                       # a few renew ticks, no flapping
    assert events == ["gain"]
    await e.stop()
    assert events == ["gain", "lose"] and not e.is_leader


# ── lock-file fallback: never "everyone holds the host lock" ──────────────────

@pytest.mark.asyncio
async def test_unopenable_lock_path_falls_back_to_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(LeaderLock.tempfile, "gettempdir", lambda: str(tmp_path))
    bad = "/proc/definitely/not/writable/a.lock"
    f1, f2 = LeaderLock.FileLock(bad), LeaderLock.FileLock(bad)
    assert f1.acquire() is True and not f1.degraded
    assert f1.path == str(tmp_path / "a.lock")
    # a second worker falls back to the SAME temp file -> genuine mutual exclusion
    assert f2.acquire() is False
    f1.release()
    assert f2.acquire() is True
    f2.release()


@pytest.mark.asyncio
async def test_no_lock_file_anywhere_leads_only_with_redis_lock(tmp_path, clock, monkeypatch):
    def _no_open(*a, **k):
        raise OSError("read-only fs")
    monkeypatch.setattr(LeaderLock.os, "open", _no_open)
    r = FakeRedis(clock)
    a, _ = _elector(tmp_path, r, lockfile="x.lock")
    b, _ = _elector(tmp_path, r, lockfile="x.lock")
    assert a._file.acquire() and a._file.degraded
    # both workers 'hold' the (nonexistent) host lock, Redis decides: exactly one leads
    res = [await a.tick(), await b.tick()]
    assert res.count(True) == 1
    await a.stop(); await b.stop()


@pytest.mark.asyncio
async def test_no_lock_file_and_redis_down_means_nobody_leads(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(LeaderLock.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    # Redis not configured at all
    a, _ = _elector(tmp_path, None)
    assert await a.tick() is False
    # Redis configured but erroring
    r = FakeRedis(clock); r.down = True
    b, _ = _elector(tmp_path, r)
    assert await b.tick() is False
    await a.stop(); await b.stop()
