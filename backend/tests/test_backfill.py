"""Tests for the safe active-time inflation backfill.

Pure-logic tests (no DB) plus a full DB round-trip: seed idle/lock windows +
window_change slices, dry-run, apply (zeroing only the overlapping slices),
verify the rest is untouched + originals are backed up, then revert and confirm
the exact prior state is restored."""
import uuid

import pytest
from sqlalchemy import text as sa_text

from app.core.backfill import pair_windows, merge_windows, slices_to_zero
from app.maintenance.idle_backfill import run_backfill, clamp_oversized, revert, BACKUP_TABLE


# ── Pure logic ────────────────────────────────────────────────────────────────

def test_pair_windows_basic():
    assert pair_windows([(10, True), (20, False)]) == [(10, 20)]


def test_pair_windows_drops_unclosed_and_ignores_unmatched_close():
    # trailing open with no close → dropped (never extrapolate an end)
    assert pair_windows([(10, True)]) == []
    # close with nothing open → ignored
    assert pair_windows([(20, False)]) == []
    # nested opens keep the earliest open
    assert pair_windows([(10, True), (12, True), (20, False)]) == [(10, 20)]


def test_merge_windows_overlap():
    assert merge_windows([(0, 10), (5, 15), (30, 40)]) == [(0, 15), (30, 40)]


def test_slices_to_zero_membership_inclusive():
    windows = [(100, 200)]
    wcs = [("a", 99), ("b", 100), ("c", 150), ("d", 200), ("e", 201)]
    assert set(slices_to_zero(wcs, windows)) == {"b", "c", "d"}  # boundaries inclusive


def test_slices_to_zero_no_windows():
    assert slices_to_zero([("a", 5)], []) == []


# ── DB round-trip ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_zeroes_only_overlapping_then_reverts(db_session):
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"BF-{tag}", "t": tag})
    await db_session.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, is_active) "
        "VALUES (:i,:c,:h,'darwin',TRUE)"),
        {"i": str(did), "c": str(cid), "h": f"h-{tag}"})

    A, B, C = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    def ins(eid, etype, mins_ago, dur=0):
        return db_session.execute(sa_text(
            "INSERT INTO activity_events (id, device_id, event_type, app_name, duration, timestamp) "
            "VALUES (:i,:d,:t,:a,:dur, NOW() - make_interval(mins => :m))"),
            {"i": eid, "d": str(did), "t": etype, "a": "Slack", "dur": dur, "m": mins_ago})

    # idle window [100m, 90m]; lock window [80m, 70m]
    await ins(str(uuid.uuid4()), "idle_start", 100)
    await ins(str(uuid.uuid4()), "idle_end", 90)
    await ins(str(uuid.uuid4()), "lock", 80)
    await ins(str(uuid.uuid4()), "unlock", 70)
    # A inside idle (zero), B inside lock (zero), C outside (keep)
    await ins(A, "window_change", 95, 600)
    await ins(B, "window_change", 75, 300)
    await ins(C, "window_change", 50, 400)
    await db_session.flush()

    async def dur_of(eid):
        r = await db_session.execute(
            sa_text("SELECT duration FROM activity_events WHERE id = :i"), {"i": eid})
        return r.scalar()

    # DRY RUN: reports 2 slices / 900s, changes nothing.
    dry = await run_backfill(db_session, company_id=cid, apply=False)
    assert dry["slices"] == 2 and dry["seconds_removed"] == 900 and dry["applied"] is False
    assert await dur_of(A) == 600 and await dur_of(B) == 300 and await dur_of(C) == 400

    # APPLY: only the idle/lock-overlapping slices are zeroed.
    applied = await run_backfill(db_session, company_id=cid, apply=True)
    assert applied["applied"] is True and applied["slices"] == 2
    assert await dur_of(A) == 0 and await dur_of(B) == 0
    assert await dur_of(C) == 400                       # outside windows → untouched
    # Scope to THIS test's ids — the shared backup table may hold rows from a
    # real prior --apply on the same DB; we only assert about our own slices.
    backup = await db_session.execute(
        sa_text(f"SELECT id::text, original_duration FROM {BACKUP_TABLE} "
                "WHERE id::text = ANY(:ids) ORDER BY original_duration"),
        {"ids": [A, B]})
    assert [(r[0], r[1]) for r in backup.fetchall()] == [(B, 300), (A, 600)]

    # REVERT: exact prior durations restored, our backup rows cleared.
    rev = await revert(db_session)
    assert rev["restored"] >= 2
    assert await dur_of(A) == 600 and await dur_of(B) == 300 and await dur_of(C) == 400
    empty = await db_session.execute(
        sa_text(f"SELECT COUNT(*) FROM {BACKUP_TABLE} WHERE id::text = ANY(:ids)"),
        {"ids": [A, B]})
    assert empty.scalar() == 0


# ── ingest cap (pure) ──────────────────────────────────────────────────────────

def test_capped_duration_clamps_window_change_only():
    from app.api.v1.endpoints.ingest import capped_duration
    assert capped_duration("window_change", 40000, 300) == 300   # sleep/offline inflation
    assert capped_duration("window_change", 120, 300) == 120     # normal slice untouched
    assert capped_duration("idle_end", 40000, 300) == 40000      # long idle is legitimate
    assert capped_duration("meeting_end", 7200, 300) == 7200     # long meeting is legitimate
    assert capped_duration("lock", 50000, 300) == 50000          # long lock is legitimate


# ── clamp_oversized round-trip ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clamp_oversized_then_revert(db_session):
    cid = uuid.uuid4(); did = uuid.uuid4(); tag = uuid.uuid4().hex[:8]
    await db_session.execute(sa_text(
        "INSERT INTO companies (id, name, enrollment_token) VALUES (:i,:n,:t)"),
        {"i": str(cid), "n": f"CLP-{tag}", "t": tag})
    await db_session.execute(sa_text(
        "INSERT INTO devices (id, company_id, hostname, platform, is_active) "
        "VALUES (:i,:c,:h,'darwin',TRUE)"),
        {"i": str(did), "c": str(cid), "h": f"h-{tag}"})

    O, N = str(uuid.uuid4()), str(uuid.uuid4())   # O = oversized, N = normal
    for eid, dur in ((O, 40000), (N, 120)):
        await db_session.execute(sa_text(
            "INSERT INTO activity_events (id, device_id, event_type, app_name, duration, timestamp) "
            "VALUES (:i,:d,'window_change','Microsoft Edge',:dur, NOW())"),
            {"i": eid, "d": str(did), "dur": dur})
    await db_session.flush()

    async def dur_of(eid):
        r = await db_session.execute(
            sa_text("SELECT duration FROM activity_events WHERE id = :i"), {"i": eid})
        return r.scalar()

    dry = await clamp_oversized(db_session, company_id=cid, cap=300, apply=False)
    assert dry["clamp_slices"] == 1 and dry["applied"] is False
    assert await dur_of(O) == 40000 and await dur_of(N) == 120        # nothing changed

    applied = await clamp_oversized(db_session, company_id=cid, cap=300, apply=True)
    assert applied["applied"] is True and applied["clamp_slices"] == 1
    assert await dur_of(O) == 300          # oversized clamped to cap
    assert await dur_of(N) == 120          # normal slice untouched
    bk = await db_session.execute(
        sa_text(f"SELECT original_duration FROM {BACKUP_TABLE} WHERE id::text = :i"), {"i": O})
    assert bk.scalar() == 40000            # true original preserved

    await revert(db_session)
    assert await dur_of(O) == 40000        # exact restore
