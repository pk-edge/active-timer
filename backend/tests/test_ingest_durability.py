"""Durability of the ingest worker failure path — no silent loss.

These exercise the pure (non-DB) parts of the recovery logic: transient-vs-data
error classification, uncounted requeue for transient errors, and the
poison-pill cap that dead-letters (logs) rather than spins forever.
"""
import asyncio

from sqlalchemy.exc import OperationalError, IntegrityError

from app.core.ingest_queue import IngestQueue, MAX_WRITE_RETRIES


def _item(i=0):
    return {"device_id": "dev-1", "row": {"id": f"e{i}", "device_id": "dev-1",
            "event_type": "window_change", "timestamp": None}}


def test_is_transient_classification():
    q = IngestQueue()
    transient = OperationalError("stmt", {}, Exception("server closed the connection"))
    data_err = IntegrityError("stmt", {}, Exception("null value in column"))
    assert q._is_transient(transient) is True
    assert q._is_transient(data_err) is False


def test_requeue_uncounted_never_drops_for_transient():
    q = IngestQueue()
    batch = [_item(i) for i in range(5)]
    # Uncounted requeue (transient path) puts everything back, no attempt bump.
    n = q._requeue(batch, count_attempt=False)
    assert n == 5
    assert q._q.qsize() == 5
    for item in batch:
        assert "attempts" not in item        # transient retries are NOT counted


def test_requeue_counted_dead_letters_after_cap(caplog=None):
    q = IngestQueue()
    item = _item(0)
    # Drive a single row past the retry cap via the counted path.
    for _ in range(MAX_WRITE_RETRIES):
        assert q._requeue([item], count_attempt=True) == 1
    # Next counted requeue exceeds the cap → dead-lettered (logged), NOT requeued.
    depth_before = q._q.qsize()
    assert q._requeue([item], count_attempt=True) == 0
    assert q._q.qsize() == depth_before        # not re-added
    assert item["attempts"] > MAX_WRITE_RETRIES


def test_requeue_dead_letters_when_queue_full():
    q = IngestQueue()
    # Shrink the queue to capacity 1 to force QueueFull on the 2nd item.
    q._q = asyncio.Queue(maxsize=1)
    n = q._requeue([_item(0), _item(1)], count_attempt=False)
    assert n == 1                              # only one fit; the other dead-lettered, not lost silently
    assert q._q.qsize() == 1
