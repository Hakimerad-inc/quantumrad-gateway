"""Unit tests for the spool store-and-forward state machine (PRD §3.3)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import DICOMDestination
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


def test_receive_transition(spool: Spool) -> None:
    study_id = spool.receive("1.2.3.4", accession="A1", modality="CT")
    assert spool.state(study_id) == StudyState.RECEIVED
    row = spool._db.get_study(study_id)
    assert row["accession"] == "A1"


def test_receive_enqueue_queued(spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    assert spool.state(study_id) == StudyState.QUEUED
    routes = spool._db.get_routes(study_id)
    assert len(routes) == 1
    assert routes[0]["target_name"] == "hub"


def test_full_cycle_to_sent(spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    tasks = spool.claim_next(limit=1)
    assert len(tasks) == 1
    assert spool.state(study_id) == StudyState.SENDING

    spool.complete(study_id, "hub")
    assert spool.state(study_id) == StudyState.SENT


def test_multi_destination_sent_only_when_all_complete(
    spool: Spool, target_hub: DICOMDestination, target_pacs: DICOMDestination
) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, target_pacs])
    spool.claim_next(limit=1)

    spool.complete(study_id, "hub")
    # PACS still pending -> not SENT yet
    assert spool.state(study_id) == StudyState.SENDING

    spool.complete(study_id, "pacs")
    assert spool.state(study_id) == StudyState.SENT


def test_error_then_retry_then_failed(spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    spool.claim_next(limit=1)
    assert spool.fail(study_id, "hub", "connection refused", max_attempts=3) == StudyState.ERROR
    assert spool.route_attempts(study_id, "hub") == 1

    # Re-forward puts it back in the queue
    spool.reforward(study_id, "hub")
    assert spool.state(study_id) == StudyState.QUEUED

    # Exhaust the remaining attempts -> FAILED (local copy retained).
    # attempts: 1 (above) + 2 more cycles = 3 == max_attempts.
    for _ in range(2):
        spool.reforward(study_id, "hub")
        spool.claim_next(limit=1)
        spool.fail(study_id, "hub", "still down", max_attempts=3)
    assert spool.state(study_id) == StudyState.FAILED


def test_disabled_target_is_skipped(spool: Spool, target_hub: DICOMDestination) -> None:
    target_hub.enabled = False
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    routes = spool._db.get_routes(study_id)
    assert routes == []
    assert spool.state(study_id) == StudyState.QUEUED


def test_claim_next_concurrency_isolation(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    first = spool.claim_next(limit=1)
    second = spool.claim_next(limit=1)
    assert len(first) == 1
    # Second claim must not re-claim the already-sending task
    assert len(second) == 0


def test_unknown_target_complete_raises(spool: Spool) -> None:
    study_id = spool.receive("1.2.3.4")
    with pytest.raises(KeyError):
        spool.complete(study_id, "nope")


# ══════════════════════════════════════════════════════════════════════
# Pipeline read queries (count_routes_by_target / list_recent_routes /
# list_audit_for_study)
# ══════════════════════════════════════════════════════════════════════


def test_count_routes_by_target(spool: Spool, target_hub: DICOMDestination) -> None:
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)
    spool.complete(study_id, "hub")

    study2 = spool.receive("1.2.3.5")
    spool.enqueue(study2, [target_hub])
    spool.claim_next(limit=1)
    spool.fail(study2, "hub", error="boom")

    rollup = {
        (row["target_name"], row["status"]): row["n"]
        for row in spool._db.count_routes_by_target()
    }
    assert rollup[("hub", "complete")] == 1
    assert rollup[("hub", "error")] == 1


def test_count_studies_since(spool: Spool) -> None:
    """The "received last hour" figure counts in SQL against created_at."""
    recent_id = spool.receive("1.2.3.4")
    old_id = spool.receive("1.2.3.5")
    # Backdate one study outside the hour window (created_at is UTC text).
    spool.database.connection().execute(
        "UPDATE studies SET created_at = ? WHERE id IN (?, ?)",
        ("2000-01-01 00:00:00", recent_id, old_id),
    )

    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert spool.database.count_studies_since(an_hour_ago) == 0
    assert spool.database.count_studies_since("2000-01-01 00:00:00") == 2
    assert spool.database.count_studies_since("2999-01-01 00:00:00") == 0


def test_pipeline_aggregates_use_covering_indexes(spool: Spool) -> None:
    """The pipeline aggregates answer from indexes, not full table scans.

    /api/pipeline is polled continuously and these aggregates take the same
    lock the receive path needs, so a plan that falls back to scanning the
    tables is the regression these tests exist to catch.
    """
    conn = spool.database.connection()

    def uses_index(sql: str, index: str) -> bool:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        return any(index in row[3] for row in plan)

    assert uses_index(
        "SELECT COUNT(*) FROM studies WHERE created_at >= '2000-01-01 00:00:00'",
        "idx_studies_created_at",
    )
    assert uses_index(
        """
        SELECT target_name, target_type, status, COUNT(*), MAX(updated_at)
        FROM task_routing GROUP BY target_name, target_type, status
        """,
        "idx_task_routing_target",
    )
    assert uses_index(
        """
        SELECT COALESCE(SUM(m.num_bytes), 0) FROM instance_meta AS m
        JOIN studies AS s ON s.study_uid = m.study_uid
        """,
        "idx_instance_meta_bytes",
    )


def test_list_recent_routes_orders_and_joins(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    study_id = spool.receive("1.2.3.4", accession="A1", modality="CT")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)

    rows = spool._db.list_recent_routes("hub")
    assert len(rows) == 1
    assert rows[0]["accession"] == "A1"
    assert rows[0]["modality"] == "CT"
    assert rows[0]["status"] == "sending"
    assert rows[0]["study_uid"] == "1.2.3.4"
    assert spool._db.list_recent_routes("nope") == []


def test_list_audit_for_study_matches_uid(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    spool._audit = audit
    study_id = spool.receive("1.2.840.10008.99.1", accession="A1")
    spool.enqueue(study_id, [target_hub])

    events = spool._db.list_audit_for_study("1.2.840.10008.99.1")
    names = {row["event"] for row in events}
    assert "STUDY_RECEIVED" in names
    assert "STUDY_QUEUED" in names
    # An unrelated study UID must match nothing.
    assert spool._db.list_audit_for_study("9.9.9.9") == []


def test_list_audit_for_study_rejects_prefix_nested_uid(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """A UID that *contains* another as a prefix must not leak its events.

    DICOM UIDs are prefix-nested: ``1.2.3`` is a substring of ``1.2.3.4``.
    The old ``LIKE '%<uid>%'`` match on the detail JSON attached the shorter
    study's events to the longer one (review M7); the exact ``study_uid``
    column lookup must not.
    """
    from mercure_gateway.audit import AuditLog

    audit = AuditLog(spool._db)
    spool._audit = audit
    short_id = spool.receive("1.2.3", accession="SHORT")
    long_id = spool.receive("1.2.3.4", accession="LONG")
    assert short_id != long_id

    long_events = spool._db.list_audit_for_study("1.2.3.4")
    assert {row["event"] for row in long_events} == {"STUDY_RECEIVED"}
    assert all(row["study_uid"] == "1.2.3.4" for row in long_events)

    short_events = spool._db.list_audit_for_study("1.2.3")
    assert {row["event"] for row in short_events} == {"STUDY_RECEIVED"}
    assert all(row["study_uid"] == "1.2.3" for row in short_events)


def test_v4_audit_events_backfilled_on_open(tmp_path):  # type: ignore[no-untyped-def]
    """A pre-v5 database's audit rows get exact study_uid values on open."""
    import sqlite3

    from mercure_gateway.spool.db import open_database

    db_path = tmp_path / "spool.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE audit_events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            event   TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT '{}',
            user    TEXT,
            hash    TEXT NOT NULL
        );
        INSERT INTO audit_events (event, detail, hash) VALUES
            ('STUDY_RECEIVED', '{"study_uid":"1.2.3.4"}', 'h1'),
            ('STUDY_QUEUED', '{"study_uid":"1.2.3.4"}', 'h2'),
            ('PRUNE_AUDIT', '{"pruned":0}', 'h3');
        """
    )
    conn.commit()
    conn.close()

    db = open_database(db_path)
    rows = db.list_audit_for_study("1.2.3.4")
    assert {row["event"] for row in rows} == {"STUDY_RECEIVED", "STUDY_QUEUED"}
    # Events without a study_uid in their detail stay NULL and match nothing.
    assert db.list_audit_for_study("1.2.3") == []
    db.close()


def test_v4_backfill_survives_append_only_triggers(tmp_path):  # type: ignore[no-untyped-def]
    """The v5 backfill must not trip the real database's append-only triggers.

    Every real v4 database carries the ``audit_events_no_update`` /
    ``audit_events_no_delete`` triggers (SCHEMA_SQL appends them), so the
    backfill UPDATE aborts with ``sqlite3.IntegrityError: audit_events is
    append-only`` unless the migration drops and recreates them — found live
    when booting against an existing spool.
    """
    import sqlite3

    from mercure_gateway.spool.db import _AUDIT_NO_DELETE, _AUDIT_NO_UPDATE, open_database

    db_path = tmp_path / "spool.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE audit_events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            event   TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT '{}',
            user    TEXT,
            hash    TEXT NOT NULL
        );
        """
    )
    conn.execute(_AUDIT_NO_UPDATE)
    conn.execute(_AUDIT_NO_DELETE)
    conn.execute(
        "INSERT INTO audit_events (event, detail, hash) VALUES "
        "('STUDY_RECEIVED', '{\"study_uid\":\"1.2.3.4\"}', 'h1'),"
        "('PRUNE_AUDIT', '{\"pruned\":0}', 'h2')"
    )
    conn.commit()
    conn.close()

    db = open_database(db_path)
    assert {row["event"] for row in db.list_audit_for_study("1.2.3.4")} == {"STUDY_RECEIVED"}
    # The append-only guard must be back in place after the migration.
    names = {
        r[0]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "audit_events_no_update" in names
    assert "audit_events_no_delete" in names
    db.close()


# ══════════════════════════════════════════════════════════════════════
# Atomicity of the complete()/fail() write path (review P2-x)
# ══════════════════════════════════════════════════════════════════════


def _trace_begins(spool: Spool, call: Callable[[], object]) -> list[str]:
    """Run *call* and return every ``BEGIN`` the connection issued.

    sqlite3's trace callback is the only way to count transactions from
    outside the Database. A test that only asserts the resulting state
    cannot distinguish one transaction from four — this can.
    """
    begins: list[str] = []

    def trace(sql: str) -> None:
        if sql.startswith("BEGIN"):
            begins.append(sql)

    spool._db._conn.set_trace_callback(trace)
    try:
        call()
    finally:
        spool._db._conn.set_trace_callback(None)
    return begins


def test_complete_on_last_route_is_one_transaction(
    spool: Spool, target_hub: DICOMDestination, target_pacs: DICOMDestination
) -> None:
    """The final complete() commits as ONE transaction, not four.

    The old code committed mark_route_sent, set_study_state,
    set_retention_delivered and the audit append separately, so between
    commits another thread could read ``route = complete`` on a study still
    SENDING. Nested ``Database.transaction`` calls join the outer transaction
    instead of recursing, so wrapping the writes makes them a single BEGIN.
    """
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, target_pacs])
    spool.claim_next(limit=2)
    spool.complete(study_id, "hub")  # not the last route -> stays SENDING

    begins = _trace_begins(spool, lambda: spool.complete(study_id, "pacs"))

    assert spool.state(study_id) == StudyState.SENT
    assert len(begins) == 1, f"expected one BEGIN for the final complete, got {begins}"


def test_fail_is_one_transaction(spool: Spool, target_hub: DICOMDestination) -> None:
    """mark_route_error and the study state commit together."""
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)

    begins = _trace_begins(
        spool, lambda: spool.fail(study_id, "hub", "connection refused", max_attempts=3)
    )

    assert spool.state(study_id) == StudyState.ERROR
    assert len(begins) == 1, f"expected one BEGIN for fail, got {begins}"


def test_fail_terminal_on_first_call_when_budget_is_one(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    """``max_attempts=1`` must reach FAILED on the FIRST call.

    The comparison uses the attempt count as of the claim (claim_next bumps
    ``attempts`` when it takes the route), read before the write transaction.
    That ordering is load-bearing: test_forwarder.py and test_audit_coverage.py
    both assert FAILED-on-first for ``max_attempts=1``, so it is re-asserted
    here against the reworked method.
    """
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)

    assert spool.fail(study_id, "hub", "boom", max_attempts=1) == StudyState.FAILED


def test_requeue_complete_routes_is_one_transaction(
    spool: Spool, target_hub: DICOMDestination, target_pacs: DICOMDestination
) -> None:
    """A re-opened study's complete routes all reset in one commit.

    The intent was an ``executemany`` in the shape of
    ``Database.claim_next_tasks`` (one statement for N routes), but the resets
    join one outer transaction instead — the observable property is the same:
    no partially-requeued study is visible to a concurrent claim between two
    route resets.
    """
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, target_pacs])
    spool.claim_next(limit=2)
    spool.complete(study_id, "hub")
    spool.complete(study_id, "pacs")
    # A new instance re-opens the study (the store upsert demotes it to
    # RECEIVED) — simulate that state change without the filesystem.
    spool._db.set_study_state(study_id, StudyState.RECEIVED.value)

    begins = _trace_begins(spool, lambda: spool._requeue_complete_routes(study_id))

    assert len(begins) == 1, f"expected one BEGIN for the requeue, got {begins}"
    statuses = {r["target_name"]: r["status"] for r in spool._db.get_routes(study_id)}
    assert statuses == {"hub": "waiting", "pacs": "waiting"}


def test_concurrent_complete_of_two_routes_lands_one_sent(tmp_path: Path) -> None:
    """Two threads completing the two routes of one study at the same time.

    Under the old per-write commits the interleaving could leave the study
    SENDING after both routes were complete, or let both threads see the
    study as not-yet-SENT. Each complete() now holds BEGIN IMMEDIATE across
    its whole write path, so the two serialize and exactly one of them
    observes the last route completing — one SENT state, one STUDY_SENT.
    """
    from test_storage import make_dataset, make_spool

    spool = make_spool(tmp_path, auto_enqueue_delay_sec=600.0)
    try:
        audit = AuditLog(spool._db)
        spool._audit = audit
        study_id = spool.store_instance(make_dataset("1.2.3.4.5"))
        hub = DICOMDestination(
            name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
        )
        pacs = DICOMDestination(
            name="pacs", type="dicom", host="pacs.local", port=104, aet_target="PACS"
        )
        spool.enqueue(study_id, [hub, pacs])
        spool.claim_next(limit=2)

        release = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def complete(target: str) -> None:
            try:
                release.wait()
                spool.complete(study_id, target)
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=complete, args=(t,)) for t in ("hub", "pacs")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == [], f"concurrent complete raised: {errors}"
        assert spool.state(study_id) == StudyState.SENT
        statuses = {r["target_name"]: r["status"] for r in spool._db.get_routes(study_id)}
        assert statuses == {"hub": "complete", "pacs": "complete"}
        # Exactly one thread took the last route and emitted the terminal event.
        assert len(spool._db.list_audit_events("STUDY_SENT")) == 1
    finally:
        spool.stop()
