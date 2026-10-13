"""Cross-worker invalidation of the in-process auth caches (Redis pub/sub).

Two 'workers' = two independent cache pairs + two bus instances wired to one
fake broker, exactly the production topology (per-process caches, shared Redis).
"""
import asyncio

import pytest
import pytest_asyncio

from app.core import AuthInvalidationBus as bus_mod
from app.core.AdminAuthCache import AdminAuthCache
from app.core.AuthInvalidationBus import AuthInvalidationBus
from app.core.device_auth_cache import DeviceAuthCache


class FakePubSub:
    def __init__(self, broker):
        self.broker = broker
        self.q: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, channel):
        self.broker.subs.setdefault(channel, []).append(self)

    async def listen(self):
        while True:
            data = await self.q.get()
            if data is None:
                return
            yield {"type": "message", "data": data}


class FakeBroker:
    """publish() side (cache client) + pubsub() side, in one object."""
    def __init__(self):
        self.subs: dict[str, list[FakePubSub]] = {}
        self.published: list[tuple[str, str]] = []
        self.fail_publish = False

    async def publish(self, channel, message):
        if self.fail_publish:
            raise ConnectionError("redis down")
        self.published.append((channel, message))
        for sub in self.subs.get(channel, []):
            sub.q.put_nowait(message)


class Worker:
    def __init__(self, broker):
        self.device = DeviceAuthCache(60, 100, negative_ttl_secs=60)
        self.admin = AdminAuthCache(60, negative_ttl_secs=60)
        self.broker = broker

        async def factory():
            ps = FakePubSub(broker)
            async def close():
                ps.q.put_nowait(None)
            return ps, close

        self.bus = AuthInvalidationBus(self.device, self.admin,
                                       redis_getter=lambda: broker, pubsub_factory=factory)

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)


@pytest_asyncio.fixture
async def two_workers():
    broker = FakeBroker()
    a, b = Worker(broker), Worker(broker)
    await a.bus.start(); await b.bus.start()
    await a.settle()
    yield a, b, broker
    await a.bus.stop(); await b.bus.stop()


@pytest.mark.asyncio
async def test_device_revoke_on_one_worker_drops_it_on_the_sibling(two_workers, monkeypatch):
    a, b, broker = two_workers
    for w in (a, b):
        w.device.set("tok-1", "dev-1")
        w.device.set_negative("tok-neg", "dev-1")
        w.device.set("tok-2", "dev-2")
    # what the module-level device_auth_cache.invalidate_device() does on worker A
    a.device.invalidate_device("dev-1")
    await a.bus.publish_device("dev-1")
    await b.settle()
    assert a.device.get("tok-1") is None and b.device.get("tok-1") is None
    assert not b.device.is_negative("tok-neg")
    assert b.device.get("tok-2") == "dev-2"            # other devices untouched
    # the sibling's epoch advanced too: an in-flight cold lookup can't re-warm it
    seen = 0
    b.device.set("tok-1", "dev-1", seen_epoch=seen)
    assert b.device.get("tok-1") is None


@pytest.mark.asyncio
async def test_admin_invalidate_all_is_broadcast_via_the_hook(two_workers):
    a, b, _ = two_workers
    for w in (a, b):
        w.admin.set("jwt", object())
        w.admin.set_negative("jwt-neg")
    a.admin.invalidate_all()                           # sync call, like users.py / roles.py
    await b.settle()
    assert a.admin.get("jwt") is None
    assert b.admin.get("jwt") is None and not b.admin.is_negative("jwt-neg")


@pytest.mark.asyncio
async def test_received_invalidation_does_not_republish(two_workers):
    a, b, broker = two_workers
    a.admin.invalidate_all()
    await a.settle(); await b.settle()
    assert len(broker.published) == 1                  # B applied it locally, no echo/loop


@pytest.mark.asyncio
async def test_own_message_is_idempotent_and_skipped(two_workers):
    a, _, _ = two_workers
    a.device.set("tok", "dev-1")
    msg = '{"k":"device","id":"dev-1","w":"%s"}' % a.bus.worker_id
    assert a.bus.handle(msg) is False                  # skipped (already applied synchronously)
    assert a.device.get("tok") == "dev-1"
    # applying a foreign message twice is harmless
    foreign = '{"k":"device","id":"dev-1","w":"someone-else"}'
    assert a.bus.handle(foreign) is True and a.bus.handle(foreign) is True
    assert a.device.get("tok") is None


@pytest.mark.asyncio
async def test_publish_failure_never_breaks_the_mutation(two_workers):
    a, _, broker = two_workers
    broker.fail_publish = True
    a.admin.set("jwt", object())
    a.admin.invalidate_all()                           # must not raise
    await a.settle()
    assert a.admin.get("jwt") is None                  # local invalidation still happened
    await a.bus.publish_device("dev-1")                # must not raise either


@pytest.mark.asyncio
async def test_no_redis_is_a_noop():
    dev, adm = DeviceAuthCache(60, 10), AdminAuthCache(60)
    bus = AuthInvalidationBus(dev, adm, redis_getter=lambda: None)
    await bus.publish_device("x")
    await bus.publish_admin()
    await bus.start()                                  # no listener without Redis
    assert bus._task is None
    adm.invalidate_all()                               # hook fires, publish is a no-op
    await bus.stop()


def test_malformed_messages_are_ignored():
    bus = AuthInvalidationBus(DeviceAuthCache(60, 10), AdminAuthCache(60), redis_getter=lambda: None)
    for raw in ("not json", "{}", '{"k":"device"}', '{"k":"weird","w":"z"}', b"\xff"):
        assert bus.handle(raw) is False


@pytest.mark.asyncio
async def test_start_stop_idempotent_and_unhooks(two_workers):
    a, _, _ = two_workers
    t = a.bus._task
    await a.bus.start()
    assert a.bus._task is t
    await a.bus.stop(); await a.bus.stop()
    assert a.admin.on_invalidate is None


@pytest.mark.asyncio
async def test_reconnect_after_a_gap_flushes_local_caches():
    broker = FakeBroker()
    w = Worker(broker)
    calls = {"n": 0}
    orig = w.bus._pubsub_factory

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("boom")              # first connect fails -> backoff -> retry
        return await orig()
    w.bus._pubsub_factory = flaky
    w.device.set("tok", "dev-1"); w.admin.set("jwt", object())
    real_sleep = asyncio.sleep
    async def fast_sleep(_):
        await real_sleep(0)
    bus_mod.asyncio.sleep = fast_sleep
    try:
        await w.bus.start()
        for _ in range(20):
            await real_sleep(0)
    finally:
        bus_mod.asyncio.sleep = real_sleep
    assert calls["n"] >= 2
    assert w.device.get("tok") is None and w.admin.get("jwt") is None   # gap => flush
    await w.bus.stop()


@pytest.mark.asyncio
async def test_module_level_invalidate_device_publishes(monkeypatch):
    """device_auth_cache.invalidate_device() (the helper endpoints call) broadcasts."""
    from app.core import device_auth_cache as mod
    sent = []

    async def fake_publish(device_id):
        sent.append(device_id)
    import app.core.RedisCache as rc
    async def noop(*a, **k): return None
    monkeypatch.setattr(bus_mod.auth_bus, "publish_device", fake_publish)
    monkeypatch.setattr(rc, "cache_set", noop)
    monkeypatch.setattr(rc, "cache_delete", noop)
    mod.device_auth_cache.set("tok-x", "dev-x")
    await mod.invalidate_device("dev-x")
    assert sent == ["dev-x"] and mod.device_auth_cache.get("tok-x") is None
