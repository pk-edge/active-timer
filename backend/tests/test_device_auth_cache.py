"""Unit tests for the in-process device-auth cache (no DB, no Redis)."""
import time

from app.core.device_auth_cache import DeviceAuthCache


def test_hit_and_miss():
    c = DeviceAuthCache(ttl_secs=60, max_entries=100)
    assert c.get("tok-unknown") is None          # cold miss
    c.set("tok-a", "dev-1")
    assert c.get("tok-a") == "dev-1"              # warm hit


def test_ttl_expiry():
    c = DeviceAuthCache(ttl_secs=0.05, max_entries=100)
    c.set("tok-a", "dev-1")
    assert c.get("tok-a") == "dev-1"
    time.sleep(0.06)
    assert c.get("tok-a") is None                 # expired → miss (forces re-validate)


def test_invalidate_device_drops_all_its_tokens():
    c = DeviceAuthCache(ttl_secs=60, max_entries=100)
    c.set("tok-old", "dev-1")
    c.set("tok-new", "dev-1")                      # same device, rotated token
    c.set("tok-other", "dev-2")
    c.invalidate_device("dev-1")
    assert c.get("tok-old") is None               # revoked device can't validate
    assert c.get("tok-new") is None
    assert c.get("tok-other") == "dev-2"          # unrelated device untouched


def test_max_entries_bounded():
    c = DeviceAuthCache(ttl_secs=60, max_entries=3)
    for i in range(10):
        c.set(f"tok-{i}", f"dev-{i}")
    # Never exceeds the cap — memory can't grow unbounded at 1000s of devices.
    assert c.stats()["tokens"] <= 3
    # The most recently inserted entry survives.
    assert c.get("tok-9") == "dev-9"


def test_reverse_index_cleaned_on_expiry():
    c = DeviceAuthCache(ttl_secs=0.05, max_entries=100)
    c.set("tok-a", "dev-1")
    time.sleep(0.06)
    assert c.get("tok-a") is None                 # triggers eviction of expired entry
    # device→token reverse index must not leak after eviction
    assert c.stats()["devices"] == 0


def test_set_skips_when_invalidation_raced(monkeypatch_unused=None):
    """The revocation-race guard: a cold lookup that captured an epoch before an
    admin revoke must NOT re-warm the cache after that revoke fires."""
    c = DeviceAuthCache(ttl_secs=60, max_entries=100)
    # In-flight cold lookup captures the epoch before its DB read.
    seen = c.epoch()
    # Admin revokes the device mid-lookup (token not cached yet → no-op drop, but
    # the epoch advances).
    c.invalidate_device("dev-1")
    # Lookup resumes and tries to cache the (now revoked) token.
    c.set("tok-a", "dev-1", seen_epoch=seen)
    assert c.get("tok-a") is None                 # refused — not re-warmed

    # Without a stale epoch, caching works normally.
    c.set("tok-b", "dev-2", seen_epoch=c.epoch())
    assert c.get("tok-b") == "dev-2"


def test_set_after_invalidate_is_dropped_by_later_invalidate():
    """If set() wins the race (epoch matches), a subsequent invalidate still drops
    it — the entry is now tracked in the reverse index."""
    c = DeviceAuthCache(ttl_secs=60, max_entries=100)
    c.set("tok-a", "dev-1", seen_epoch=c.epoch())
    assert c.get("tok-a") == "dev-1"
    c.invalidate_device("dev-1")
    assert c.get("tok-a") is None
