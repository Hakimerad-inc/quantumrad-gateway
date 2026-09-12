"""TD-06 (GREEN): durable hub event delivery via the spool outbox.

The streamer must keep the US-10 isolation invariant (``feed()`` never blocks)
while gaining at-least-once delivery across restarts: every event is committed
to the ``hub_outbox`` table before it is queued, delivered rows are removed,
failed rows retain their attempt counter, and a fresh streamer resumes pending
rows in insertion order on ``start()``.

Behaviors under test:
1. ``feed()`` persists every event to the outbox when a database is injected.
2. Restart (new streamer, same DB) redelivers the pending events at-least-once.
3. Confirmed delivery removes the durable rows.
4. Failed delivery keeps the rows and records the attempt counter.
5. The queue memory bound also bounds the outbox (overflow rows are pruned).
6. A persistence failure degrades to in-memory-only without blocking ``feed()``.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from mercure_gateway.hub_events import HubEventStreamer
from mercure_gateway.spool.db import mem_database

PAIR = ("1.2.826.0.1.3680043.10.150.99",)


def _waiter_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _collector(posts: list[tuple[Any, dict[str, Any]]], *, ok: bool = True) -> Any:
    def _post(*args: Any, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.ok = ok
        resp.status_code = 200 if ok else 500
        posts.append((args, kwargs))
        return resp

    return _post


def _feed(streamer: HubEventStreamer, n: int, *, prefix: str = "STUDY_RECEIVED") -> None:
    for i in range(n):
        streamer.feed(prefix, {"study_uid": PAIR[0], "idx": i})


# ══════════════════════════════════════════════════════════════════════
# Durable persistence + resume
# ══════════════════════════════════════════════════════════════════════


def test_durable_feed_persists_every_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a database injected, every feed lands in the outbox."""
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    streamer = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    streamer.start()
    try:
        _feed(streamer, 3)
        _waiter_until(lambda: db.count_pending_hub_events() >= 3)
        assert db.count_pending_hub_events() == 3
    finally:
        streamer.stop()

    # Stop leaves the events on disk; nothing was delivered (bookkeeper down).
    assert db.count_pending_hub_events() == 3


def test_restart_resumes_and_delivers_all_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh streamer over the same DB redelivers events at-least-once."""
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    first = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    first.start()
    try:
        _feed(first, 5)
        _waiter_until(lambda: first.queue_size >= 5)
    finally:
        first.stop()

    # Second streamer "after a crash": same DB, bookkeeper now reachable.
    posts: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector(posts, ok=True)
    )
    second = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    assert second.queue_size == 0
    second.start()
    try:
        second.flush(timeout=5.0)
        # All five make it through — at-least-once, none lost.
        delivered = [
            e["event"]
            for _, kwargs in posts
            for e in kwargs["json"]["events"]
            if kwargs["json"]["gateway"] == "GW-D"
        ]
        assert delivered.count("STUDY_RECEIVED") == 5
    finally:
        second.stop()

    _waiter_until(lambda: db.count_pending_hub_events() == 0)


def test_delivery_removes_durable_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmed delivery deletes the outbox rows (already-processed)."""
    posts: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector(posts, ok=True)
    )
    db = mem_database()
    streamer = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    streamer.start()
    try:
        _feed(streamer, 3)
        streamer.flush(timeout=5.0)
    finally:
        streamer.stop()
    _waiter_until(lambda: db.count_pending_hub_events() == 0)


def test_failed_delivery_keeps_rows_and_records_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undelivered events stay durably queued and the attempt counter grows."""
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    streamer = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    streamer.start()
    try:
        _feed(streamer, 3)
        _waiter_until(
            lambda: any(r["attempts"] >= 1 for r in db.load_pending_hub_events())
        )
        rows = db.load_pending_hub_events()
        assert len(rows) == 3
        assert all(r["attempts"] >= 1 for r in rows)
        assert all(r["last_error"] for r in rows)
        # Order preserved: oldest row id still first after requeues.
        assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    finally:
        streamer.stop()


def test_overflow_drops_oldest_and_prunes_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory bound also bounds the durable outbox (oldest pruned)."""
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    streamer = HubEventStreamer(
        "https://hub.test", "tk", "GW-D", database=db, max_queue_size=5
    )
    streamer.start()
    try:
        _feed(streamer, 20)
        _waiter_until(lambda: streamer.queue_size <= 5)
        assert streamer.queue_size <= 5
        assert db.count_pending_hub_events() <= 5
    finally:
        streamer.stop()


def test_outbox_cap_never_prunes_events_still_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TD-06 follow-up: the safety-net prune must not delete undelivered rows.

    The old fixed 512-row cap sat below the 1000-event memory queue: with the
    hub down and 600 events fed, feed() evicted nothing, yet the 513th insert
    DELETEd the 88 oldest still-queued rows — a crash then lost them despite
    the at-least-once guarantee. The cap must be derived from the queue bound.
    """
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    streamer = HubEventStreamer(
        "https://hub.test", "tk", "GW-D", database=db,
        max_queue_size=100, max_batch_size=20,
    )
    streamer.start()
    try:
        _feed(streamer, 80)  # below memory bound -> no eviction path taken
        _waiter_until(lambda: db.count_pending_hub_events() >= 80)
        # Old cap would have pruned at 512 here only at scale; assert the
        # derived cap is what the streamer passed, and that nothing was lost.
        assert streamer._outbox_cap == 120
        assert db.count_pending_hub_events() == 80
        ids = [r["id"] for r in db.load_pending_hub_events(limit=200)]
        assert len(ids) == 80
        assert ids == sorted(ids)  # oldest still on disk, insertion order
    finally:
        streamer.stop()


# ══════════════════════════════════════════════════════════════════════
# Isolation invariant stays intact
# ══════════════════════════════════════════════════════════════════════


def test_feed_is_fast_with_durable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistence must not block feed() (US-10 isolation invariant)."""
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    streamer = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    streamer.start()
    try:
        start = time.monotonic()
        _feed(streamer, 10)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"feed() blocked on persistence: {elapsed:.3f}s"
    finally:
        streamer.stop()


def test_persistence_failure_degrades_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing outbox write must not block or crash feed()."""
    monkeypatch.setattr(
        "mercure_gateway.hub_events.requests.post", _collector([], ok=False)
    )
    db = mem_database()
    monkeypatch.setattr(
        db, "enqueue_hub_event", MagicMock(side_effect=RuntimeError("disk full"))
    )
    streamer = HubEventStreamer("https://hub.test", "tk", "GW-D", database=db)
    streamer.start()
    try:
        start = time.monotonic()
        _feed(streamer, 5)
        elapsed = time.monotonic() - start
        # Events stayed in memory (US-10 survives a DB outage, too).
        assert streamer.queue_size >= 5
        assert elapsed < 0.5
    finally:
        streamer.stop()


def test_memory_mode_has_no_outbox_rows() -> None:
    """Without a database the streamer never touches the outbox."""
    streamer = HubEventStreamer("https://hub.test", "tk", "GW-D")
    streamer.start()
    try:
        _feed(streamer, 3)
        assert streamer.queue_size >= 3
    finally:
        streamer.stop()


# ══════════════════════════════════════════════════════════════════════
# Database-level helpers
# ══════════════════════════════════════════════════════════════════════


def test_enqueue_drop_and_load_order() -> None:
    """enqueue/delete/load round-trip preserves insertion order."""
    db = mem_database()
    ids = [db.enqueue_hub_event('{"event":"A","detail":{},"ts":"t"}') for _ in range(3)]
    assert db.count_pending_hub_events() == 3
    assert [r["id"] for r in db.load_pending_hub_events()] == ids

    db.delete_hub_events([ids[1]])
    assert db.count_pending_hub_events() == 2
    assert [r["id"] for r in db.load_pending_hub_events()] == [ids[0], ids[2]]

    # drop_oldest_id trims the oldest row atomically with the insert.
    db.enqueue_hub_event('{"event":"D","detail":{},"ts":"t"}', drop_oldest_id=ids[0])
    remaining = [r["id"] for r in db.load_pending_hub_events()]
    assert remaining == [ids[2], ids[0] + 3]
    assert ids[2] in remaining and ids[0] not in remaining