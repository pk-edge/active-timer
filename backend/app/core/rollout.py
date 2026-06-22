"""Deterministic, sticky staged-rollout bucketing.

A device's bucket is a stable hash of (device_id, target version), so:
- the same device always gets the same answer for a given target version
  (no flip-flopping as the percent ramps up),
- raising rollout_percent only ever ADDS devices (monotonic).
"""
from __future__ import annotations

import hashlib


def _bucket(device_id: str, version: str) -> int:
    h = hashlib.sha256(f"{device_id}:{version}".encode()).hexdigest()
    return int(h[:8], 16) % 100  # 0..99


def in_rollout(device_id: str, version: str, rollout_percent: int) -> bool:
    if rollout_percent >= 100:
        return True
    if rollout_percent <= 0:
        return False
    return _bucket(device_id, version) < rollout_percent
