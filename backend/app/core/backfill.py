"""Pure logic for the safe, reversible active-time backfill.

The old idle/meeting-exemption bug let `window_change` slices be recorded while
the user was actually idle or the screen was locked, inflating active-time. The
ONLY principled signal for "this slice should not have counted" is a *recorded*
idle (`idle_start`..`idle_end`) or lock (`lock`..`unlock`) window — a window_change
whose timestamp falls inside one is definitively not active work.

This module is pure (no DB, no I/O) so the windowing + membership logic can be
unit-tested exhaustively. The caller (scripts/backfill_idle_inflation.py) supplies
events from the DB, backs up originals, and zeroes the matched durations inside a
transaction. We deliberately do NOT cap by heuristic ("> N hours/day"): that would
risk destroying legitimate long-work data. Conservative-by-design — we only ever
zero slices that provably overlap recorded idle/lock, and never invent a window
end (an unmatched open is dropped, not extrapolated).
"""
from __future__ import annotations

import bisect


def pair_windows(events):
    """Pair open/close events into closed [open_ts, close_ts] windows.

    events: iterable of (ts, is_open) — is_open True for idle_start/lock,
    False for idle_end/unlock. Robust to missing markers: an unmatched close is
    ignored, nested opens keep the earliest open, and a still-open window at the
    end is dropped (we never extrapolate an end → never over-zero).
    """
    windows = []
    open_ts = None
    for ts, is_open in sorted(events, key=lambda e: e[0]):
        if is_open:
            if open_ts is None:
                open_ts = ts
        else:
            if open_ts is not None:
                if ts > open_ts:
                    windows.append((open_ts, ts))
                open_ts = None
    return windows


def merge_windows(windows):
    """Merge overlapping/adjacent [a, b] windows into a sorted, disjoint list."""
    merged = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def slices_to_zero(window_changes, windows):
    """Return ids of window_change slices whose timestamp falls within any window.

    window_changes: iterable of (id, ts). windows: iterable of (open_ts, close_ts).
    Membership is inclusive on both ends. O(n log m) via bisect over merged windows.
    """
    merged = merge_windows(windows)
    if not merged:
        return []
    starts = [a for a, _ in merged]
    out = []
    for wid, ts in window_changes:
        i = bisect.bisect_right(starts, ts) - 1  # last window starting at/<= ts
        if i >= 0 and ts <= merged[i][1]:
            out.append(wid)
    return out
