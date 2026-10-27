"""Cross-worker sync of the live device-state cache (`ingest_queue.device_cache`).

With several uvicorn workers, each agent POST updates only the in-memory
`device_cache` of the worker that happened to receive it, and that cache feeds
the realtime views (current app, passive/active status, "online right now").
Without sharing, the worker serving a dashboard would be blind to the devices
that post to its sibling until the 30 s DB flush.

This module mirrors those entries through ONE Redis hash (`tt:dstate`):
  • push: `note()` (called synchronously from IngestQueue._update_cache) buffers
    the latest state per device; every PUSH_SECS the buffer goes out via one
    Lua compare-and-set per chunk: a field is written only when the new `ls`
    (last-seen) is not older than the stored one, so a slow worker flushing an
    older state can never overwrite a newer one written by its sibling.
  • pull: every PULL_SECS the hash is read and entries NEWER than the local one
    are merged into `device_cache` — readers keep using the plain dict, with
    zero API change. Cross-worker staleness is therefore ≤ ~PULL+PUSH secs.
  • entries not updated for PRUNE_SECS are HDEL'd (removed/retired devices
    cannot accumulate) with a Lua compare-and-HDEL — only if the stored value is
    still the exact stale one we read, so a fresh state written meanwhile
    survives; the hash key itself carries a TTL.
Redis down/unset → silently per-worker (identical to single-worker behaviour).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

HASH_KEY = "tt:dstate"
# KEYS[1]=hash; ARGV[1]=hash ttl; then triplets (device_id, json_value, ls).
# Writes a field only if absent/unparsable or the incoming ls >= the stored ls.
_CAS_LUA = """
local written = 0
for i = 2, #ARGV, 3 do
  local cur = redis.call('HGET', KEYS[1], ARGV[i])
  local write = true
  if cur then
    local ok, d = pcall(cjson.decode, cur)
    if ok and type(d) == 'table' and tonumber(d['ls']) and tonumber(d['ls']) > tonumber(ARGV[i + 2]) then
      write = false
    end
  end
  if write then
    redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
    written = written + 1
  end
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return written
"""

# KEYS[1]=hash; ARGV = pairs (device_id, expected_json). HDEL only if unchanged.
_HDEL_IF_SAME_LUA = """
local n = 0
for i = 1, #ARGV, 2 do
  if redis.call('HGET', KEYS[1], ARGV[i]) == ARGV[i + 1] then
    n = n + redis.call('HDEL', KEYS[1], ARGV[i])
  end
end
return n
"""

_CHUNK = 500        # devices per Lua call (bounded ARGV size)

PUSH_SECS = 1.5
PULL_SECS = 2.0
PRUNE_SECS = 3600
HASH_TTL_SECS = 6 * 3600


class DeviceStateSync:
    def __init__(self, cache: dict, redis_getter=None):
        self._cache = cache
        self._pending: dict[str, str] = {}
        self._pending_ls: dict[str, float] = {}
        self._seen_raw: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._redis_getter = redis_getter or self._default_redis

    @staticmethod
    def _default_redis():
        from app.core.RedisClient import get_client
        return get_client()

    # Called synchronously on every enqueue — must stay O(1) and never raise.
    def note(self, device_id: str, state: dict) -> None:
        ts = state.get("last_seen")
        if ts is None:
            return
        ls = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
        self._pending_ls[device_id] = ls
        self._pending[device_id] = json.dumps({
            "ls": ls,
            "lt": state.get("latest_type"),
            "ca": state.get("current_app"),
            "ua": state.get("updated_at"),
        }, separators=(",", ":"))

    async def push(self) -> int:
        r = self._redis_getter()
        if r is None or not self._pending:
            return 0
        batch, self._pending = self._pending, {}
        lss, self._pending_ls = self._pending_ls, {}
        items = list(batch.items())
        sent = 0
        try:
            for i in range(0, len(items), _CHUNK):
                chunk = items[i:i + _CHUNK]
                args: list = [HASH_TTL_SECS]
                for did, val in chunk:
                    args += [did, val, repr(float(lss.get(did, 0.0)))]
                await asyncio.wait_for(r.eval(_CAS_LUA, 1, HASH_KEY, *args), 2.0)
                sent += len(chunk)
        except Exception as exc:  # noqa: BLE001 — stay per-worker; retry newer state next cycle
            # Re-queue everything not yet confirmed; never clobber a newer note().
            for did, val in items[sent:]:
                if did not in self._pending:
                    self._pending[did] = val
                    self._pending_ls[did] = lss.get(did, 0.0)
            log.debug("device_state_sync: push failed (%s)", exc)
            return sent
        return sent

    async def pull(self) -> int:
        r = self._redis_getter()
        if r is None:
            return 0
        try:
            raw = await asyncio.wait_for(r.hgetall(HASH_KEY), 2.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("device_state_sync: pull failed (%s)", exc)
            return 0
        merged, stale = 0, []   # stale: (device_id, raw_value_we_saw)
        cutoff = time.time() - PRUNE_SECS
        for k, v in raw.items():
            did = k.decode() if isinstance(k, bytes) else k
            sv = v.decode() if isinstance(v, bytes) else v
            if self._seen_raw.get(did) == sv:
                continue
            self._seen_raw[did] = sv
            try:
                d = json.loads(sv)
                ls = float(d["ls"])
            except Exception:  # noqa: BLE001
                continue
            if (d.get("ua") or ls) < cutoff:
                stale.append((did, sv))
                continue
            local = self._cache.get(did)
            local_ts = local.get("last_seen") if local else None
            if local_ts is not None and local_ts.timestamp() >= ls:
                continue
            self._cache[did] = {
                "last_seen":   datetime.fromtimestamp(ls, tz=timezone.utc),
                "latest_type": d.get("lt"),
                "current_app": d.get("ca"),
                "updated_at":  d.get("ua") or time.time(),
            }
            merged += 1
        if stale:
            # Compare-and-HDEL: a fresh state written since our HGETALL survives.
            try:
                for i in range(0, len(stale), _CHUNK):
                    args = [x for pair in stale[i:i + _CHUNK] for x in pair]
                    await asyncio.wait_for(r.eval(_HDEL_IF_SAME_LUA, 1, HASH_KEY, *args), 2.0)
            except Exception:  # noqa: BLE001
                pass
            for did, _sv in stale:
                self._seen_raw.pop(did, None)
        return merged

    async def _loop(self) -> None:
        last_pull = 0.0
        while True:
            await asyncio.sleep(PUSH_SECS)
            try:
                await self.push()
                if time.monotonic() - last_pull >= PULL_SECS:
                    last_pull = time.monotonic()
                    await self.pull()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("device_state_sync: cycle failed")

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="device_state_sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
