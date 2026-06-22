"""Safe, reversible backfill that removes historical active-time inflation.

Run as a module:

    # DRY RUN (default) — reports impact, changes nothing:
    python -m app.maintenance.idle_backfill
    # scope to one company / window:
    python -m app.maintenance.idle_backfill --company <uuid> --since-days 60
    # APPLY (backs up originals, then zeroes inside a transaction):
    python -m app.maintenance.idle_backfill --apply
    # REVERT a previous apply (restores from the backup table):
    python -m app.maintenance.idle_backfill --revert

Only window_change slices whose timestamp falls inside a RECORDED idle
(idle_start..idle_end) or lock (lock..unlock) window are touched — see
app/core/backfill.py for the pure logic. Every affected row's original duration
is copied to `activity_events_backfill_backup` before being zeroed, so --revert
restores the exact prior state. No heuristic capping is ever applied.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import text

from app.core.backfill import pair_windows, slices_to_zero

BACKUP_TABLE = "activity_events_backfill_backup"

_CREATE_BACKUP = text(f"""
    CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
        id               uuid PRIMARY KEY,
        original_duration integer NOT NULL,
        backfilled_at    timestamptz NOT NULL DEFAULT now()
    )
""")


async def compute(session, company_id=None, since_days=None):
    """Pure-ish read phase: returns (ids_to_zero, seconds_removed, devices_touched).
    No writes. Groups events per device and applies the pure windowing logic."""
    where = ["1=1"]
    params = {}
    if company_id:
        where.append("d.company_id = :cid")
        params["cid"] = str(company_id)
    if since_days:
        where.append("ae.timestamp >= NOW() - make_interval(days => :days)")
        params["days"] = int(since_days)
    cond = " AND ".join(where)

    markers = await session.execute(
        text(f"""
            SELECT ae.device_id::text AS did, ae.event_type AS et,
                   EXTRACT(EPOCH FROM ae.timestamp)::bigint AS ts
            FROM activity_events ae JOIN devices d ON d.id = ae.device_id
            WHERE {cond} AND ae.event_type IN ('idle_start','idle_end','lock','unlock')
            ORDER BY ae.device_id, ae.timestamp
        """),
        params,
    )
    idle_ev = defaultdict(list)   # did -> [(ts, is_open)]
    lock_ev = defaultdict(list)
    for r in markers.fetchall():
        if r.et in ("idle_start", "idle_end"):
            idle_ev[r.did].append((int(r.ts), r.et == "idle_start"))
        else:
            lock_ev[r.did].append((int(r.ts), r.et == "lock"))

    wcs = await session.execute(
        text(f"""
            SELECT ae.id::text AS id, ae.device_id::text AS did, ae.duration AS dur,
                   EXTRACT(EPOCH FROM ae.timestamp)::bigint AS ts
            FROM activity_events ae JOIN devices d ON d.id = ae.device_id
            WHERE {cond} AND ae.event_type = 'window_change' AND ae.duration > 0
            ORDER BY ae.device_id
        """),
        params,
    )
    wc_by_dev = defaultdict(list)   # did -> [(id, ts)]
    dur = {}                        # id -> duration
    for r in wcs.fetchall():
        wc_by_dev[r.did].append((r.id, int(r.ts)))
        dur[r.id] = int(r.dur or 0)

    ids, secs, devices = [], 0, 0
    for did, changes in wc_by_dev.items():
        windows = pair_windows(idle_ev.get(did, [])) + pair_windows(lock_ev.get(did, []))
        if not windows:
            continue
        hit = slices_to_zero(changes, windows)
        if hit:
            devices += 1
            ids.extend(hit)
            secs += sum(dur[i] for i in hit)
    return ids, secs, devices


async def run_backfill(session, company_id=None, since_days=None, apply=False):
    ids, secs, devices = await compute(session, company_id, since_days)
    summary = {
        "slices": len(ids),
        "seconds_removed": secs,
        "hours_removed": round(secs / 3600, 1),
        "devices_affected": devices,
        "applied": False,
    }
    if apply and ids:
        await session.execute(_CREATE_BACKUP)
        # Preserve originals (idempotent: a re-run won't clobber an earlier backup).
        await session.execute(
            text(f"""
                INSERT INTO {BACKUP_TABLE} (id, original_duration)
                SELECT id, duration FROM activity_events
                WHERE id::text = ANY(:ids)
                ON CONFLICT (id) DO NOTHING
            """),
            {"ids": ids},
        )
        await session.execute(
            text("UPDATE activity_events SET duration = 0 WHERE id::text = ANY(:ids)"),
            {"ids": ids},
        )
        summary["applied"] = True
    return summary


async def revert(session):
    """Restore every backed-up duration, then drop the backup rows used."""
    res = await session.execute(
        text(f"""
            UPDATE activity_events ae
            SET duration = b.original_duration
            FROM {BACKUP_TABLE} b
            WHERE ae.id = b.id
            RETURNING ae.id
        """)
    )
    restored = len(res.fetchall())
    await session.execute(text(f"DELETE FROM {BACKUP_TABLE}"))
    return {"restored": restored}


async def _main():
    ap = argparse.ArgumentParser(description="Safe active-time inflation backfill")
    ap.add_argument("--apply", action="store_true", help="apply (default is dry-run)")
    ap.add_argument("--revert", action="store_true", help="restore from backup table")
    ap.add_argument("--company", default=None, help="restrict to one company uuid")
    ap.add_argument("--since-days", type=int, default=None, help="only events newer than N days")
    args = ap.parse_args()

    from app.db.base import AsyncSessionLocal  # local import: app must be configured
    async with AsyncSessionLocal() as session:
        if args.revert:
            out = await revert(session)
            await session.commit()
            print(f"REVERT: restored {out['restored']} slices from backup")
            return
        out = await run_backfill(session, args.company, args.since_days, apply=args.apply)
        if args.apply:
            await session.commit()
        mode = "APPLIED" if out["applied"] else ("DRY-RUN (no changes)" if not args.apply else "nothing to do")
        print(f"[{mode}] slices={out['slices']} hours_removed={out['hours_removed']} "
              f"devices_affected={out['devices_affected']}")
        if not args.apply and out["slices"]:
            print("Re-run with --apply to zero these (originals are backed up; --revert undoes it).")


if __name__ == "__main__":
    asyncio.run(_main())
