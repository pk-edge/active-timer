"""Tiny semver helper for agent update decisions. Tolerant: returns None for
unparseable input so callers can fall back to string comparison (no regression
vs the previous string-equality update check)."""
from __future__ import annotations

import re

_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(s: str | None) -> tuple[int, int, int] | None:
    if not s:
        return None
    m = _RE.match(s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True if `candidate` should be offered as an update over `current`."""
    pc, pcur = parse_version(candidate), parse_version(current)
    if pc is not None and pcur is not None:
        return pc > pcur
    # Fallback preserves today's behavior: any difference is "newer".
    return (candidate or "") != (current or "")


def meets_minimum(version: str | None, floor: str | None) -> bool:
    """True if `version` is >= `floor`. No floor → always True."""
    if not floor:
        return True
    pv, pf = parse_version(version), parse_version(floor)
    if pv is None or pf is None:
        return True  # can't decide → don't force
    return pv >= pf
