"""Singleton loops (tech_cache / device_janitor): idempotent start/stop, isolated
background DB pool; main.make_singleton_hooks: isolated stops, clean retry."""
import asyncio

import pytest

from app.core import device_janitor, tech_cache
from app.main import make_singleton_hooks


@pytest.fixture(autouse=True)
def _park_loops(monkeypatch):
    """Replace the real loops with parked ones: nothing touches the DB."""
    async def parked():
        await asyncio.sleep(3600)
    monkeypatch.setattr(tech_cache, "_loop", parked)
    monkeypatch.setattr(device_janitor, "_loop", parked)


@pytest.mark.asyncio
@pytest.mark.parametrize("mod", [tech_cache, device_janitor])
async def test_start_twice_leaves_exactly_one_loop_and_stop_tolerates_not_started(mod):
    await mod.stop()                                   # never started: no error
    await mod.start()
    first = mod._worker_task
    await mod.start()                                  # second start: no orphaned loop
    assert mod._worker_task is first and not first.done()
    await mod.stop()
    assert first.cancelled() and mod._worker_task is None
    await mod.stop()                                   # double stop: still fine
    await mod.start()                                  # restartable after stop
    assert mod._worker_task is not first
    await mod.stop()


@pytest.mark.asyncio
async def test_crashed_loop_is_replaced_by_start_and_stop_does_not_raise(monkeypatch):
    async def boom():
        raise RuntimeError("crashed")
    monkeypatch.setattr(tech_cache, "_loop", boom)
    await tech_cache.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert tech_cache._worker_task.done()
    await tech_cache.start()                           # dead task => a fresh loop is started
    await tech_cache.stop()                            # reaps the failed task without raising


@pytest.mark.parametrize("mod", [tech_cache, device_janitor])
def test_loops_use_the_isolated_background_pool(mod):
    import inspect
    src = inspect.getsource(mod)
    assert "BackgroundSessionLocal" in src and "AsyncSessionLocal" not in src.replace(
        "BackgroundSessionLocal", "")


class Fake:
    def __init__(self, name, log, fail_start=False, fail_stop=False):
        self.name, self.log, self.fail_start, self.fail_stop = name, log, fail_start, fail_stop
        self.running = False

    async def start(self):
        if self.fail_start:
            raise RuntimeError(f"{self.name} start failed")
        self.running = True
        self.log.append(f"start:{self.name}")

    async def stop(self):
        self.log.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError(f"{self.name} stop failed")
        self.running = False


@pytest.mark.asyncio
async def test_stop_all_runs_every_stop_even_if_one_raises():
    log: list[str] = []
    mods = [Fake("a", log), Fake("b", log, fail_stop=True), Fake("c", log)]
    _, stop_all = make_singleton_hooks([(m.name, m) for m in mods])
    await stop_all()                                   # must not raise
    assert log == ["stop:c", "stop:b", "stop:a"]


@pytest.mark.asyncio
async def test_failed_start_rolls_back_so_a_retry_is_clean():
    log: list[str] = []
    a, b, c = Fake("a", log), Fake("b", log, fail_start=True), Fake("c", log)
    start_all, _ = make_singleton_hooks([("a", a), ("b", b), ("c", c)])
    with pytest.raises(RuntimeError):
        await start_all()
    assert not a.running and not c.running             # no half-started loops left behind
    b.fail_start = False
    await start_all()                                  # retry
    assert a.running and b.running and c.running
