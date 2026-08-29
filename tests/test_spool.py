"""Unit tests for the spool store-and-forward state machine (PRD §3.3)."""

from __future__ import annotations

import pytest

from mercure_gateway.config import DICOMDestination
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def target_hub() -> DICOMDestination:
    return DICOMDestination(
        name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
    )


@pytest.fixture()
def target_pacs() -> DICOMDestination:
    return DICOMDestination(
        name="pacs", type="dicom", host="pacs.local", port=104, aet_target="PACS"
    )


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
