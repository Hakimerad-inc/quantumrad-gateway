"""TDD: unit tests for the Forwarding Engine (Forwarder + handler interface).

Behaviors covered (in RED-GREEN order):
1.  A queued study with a registered handler is delivered → SENT  (tracer bullet)
2.  Unregistered target type → task fails gracefully, study → ERROR
3.  Delivery failure triggers retry; after max_attempts → FAILED
4.  Multiple destinations: all must succeed before SENT
5.  process_once returns the count of tasks actually processed
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mercure_gateway.config import (
    DICOMDestination,
    SFTPDestination,
    default_config,
)
from mercure_gateway.forwarder import DeliveryResult, Forwarder, RetryPolicy
from mercure_gateway.spool import ClaimedTask, Spool, StudyState
from mercure_gateway.spool.db import mem_database


class FakeHandler:
    """Handler that records calls and returns a configurable result."""

    def __init__(self, succeed: bool = True) -> None:
        self.calls: list[ClaimedTask] = []
        self.succeed = succeed

    def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
        self.calls.append(task)
        return DeliveryResult(ok=self.succeed)


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


@pytest.fixture()
def handler() -> FakeHandler:
    return FakeHandler(succeed=True)


@pytest.fixture()
def forwarder(handler: FakeHandler) -> Forwarder:
    spool = Spool(mem_database())
    cfg = default_config()
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(max_attempts=3))
    fwd.register_handler("dicom", handler)
    return fwd


# ── Slice 1 (tracer bullet): successful delivery ──────────────────────


def test_successful_delivery(
    forwarder: Forwarder, target_hub: DICOMDestination
) -> None:
    study_id = forwarder.spool.receive("1.2.3.4")
    forwarder.spool.enqueue(study_id, [target_hub])

    count = forwarder.process_once()

    assert count == 1
    assert forwarder.spool.state(study_id) == StudyState.SENT


# ── Slice 2: unregistered handler fails gracefully ─────────────────────


def test_unregistered_handler_fails(
    forwarder: Forwarder,
) -> None:
    # Only "dicom" is registered in the fixture. Send to an "sftp" target.
    target = SFTPDestination(
        name="nas", type="sftp", host="nas.local", port=22, username="u"
    )

    study_id = forwarder.spool.receive("1.2.3.4")
    forwarder.spool.enqueue(study_id, [target])

    count = forwarder.process_once()

    assert count == 1
    assert forwarder.spool.state(study_id) == StudyState.ERROR


# ── Slice 3: retry exhausts to FAILED ─────────────────────────────────


def test_retry_then_failed(
    target_hub: DICOMDestination,
) -> None:
    spool = Spool(mem_database())
    cfg = default_config()
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=3))
    handler = FakeHandler(succeed=False)
    fwd.register_handler("dicom", handler)

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    count = fwd.process_once()

    assert count == 1
    assert spool.state(study_id) == StudyState.FAILED
    assert len(handler.calls) == 3


# ── Slice 4: multiple destinations must all succeed before SENT ────────


def test_multi_destination_sent(
    forwarder: Forwarder,
    handler: FakeHandler,
    target_hub: DICOMDestination,
    target_pacs: DICOMDestination,
) -> None:
    study_id = forwarder.spool.receive("1.2.3.4")
    forwarder.spool.enqueue(study_id, [target_hub, target_pacs])

    # First pass processes the hub task; pacs still pending -> not SENT
    count = forwarder.process_once(limit=1)
    assert count == 1
    assert forwarder.spool.state(study_id) == StudyState.SENDING

    # Second pass processes the pacs task -> SENT
    count = forwarder.process_once(limit=1)
    assert count == 1
    assert forwarder.spool.state(study_id) == StudyState.SENT
    assert len(handler.calls) == 2


# ── Slice 5: process_once returns the number of tasks processed ────────


def test_process_once_return_count(
    forwarder: Forwarder, target_hub: DICOMDestination
) -> None:
    study1 = forwarder.spool.receive("1.2.3.1")
    study2 = forwarder.spool.receive("1.2.3.2")
    forwarder.spool.enqueue(study1, [target_hub])
    forwarder.spool.enqueue(study2, [target_hub])

    count = forwarder.process_once(limit=2)

    assert count == 2
    assert forwarder.spool.state(study1) == StudyState.SENT
    assert forwarder.spool.state(study2) == StudyState.SENT