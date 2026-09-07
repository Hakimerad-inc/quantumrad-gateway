"""Unit tests for the spool store-and-forward state machine (PRD §3.3)."""

from __future__ import annotations

import pytest

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
