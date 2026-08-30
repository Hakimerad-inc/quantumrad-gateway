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

try:  # pydicom is a runtime dep; ImportErrors only if absent in dev envs
    from pydicom.errors import InvalidDicomError
except ImportError:  # pragma: no cover
    class InvalidDicomError(Exception):  # type: ignore[no-redef]
        pass


class FakeHandler:
    """Handler that records calls and returns a configurable result."""

    def __init__(self, succeed: bool = True) -> None:
        self.calls: list[ClaimedTask] = []
        self.succeed = succeed

    def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
        self.calls.append(task)
        return DeliveryResult(ok=self.succeed)


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


def test_handler_exception_does_not_kill_loop(
    target_hub: DICOMDestination,
) -> None:
    """A crashing handler must fail the route, not propagate (one corrupt
    file or broken handler must never halt forwarding for the whole queue)."""
    spool = Spool(mem_database())
    cfg = default_config()
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=1))

    class ExplodingHandler:
        def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
            raise InvalidDicomError("corrupt file")

    fwd.register_handler("dicom", ExplodingHandler())

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    count = fwd.process_once()  # must not raise
    assert count == 1
    assert spool.state(study_id) == StudyState.FAILED


def test_retry_reclaims_same_task_not_arbitrary(
    target_hub: DICOMDestination,
) -> None:
    """After a failure the retry must re-claim the SAME route by id — not
    whatever waiting task happens to sort first (cross-task misdelivery)."""
    spool = Spool(mem_database())
    cfg = default_config()
    fwd = Forwarder(cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=2))

    attempts: list[ClaimedTask] = []

    class FailOnceHandler:
        """Fails the first task it sees, succeeds afterwards."""

        def __init__(self) -> None:
            self.seen_first: int | None = None

        def deliver(self, task: ClaimedTask, spool_dir: Path) -> DeliveryResult:
            attempts.append(task)
            if self.seen_first is None:
                self.seen_first = task.study_id
                return DeliveryResult(ok=False, error="first attempt fails")
            return DeliveryResult(ok=True)

    handler = FailOnceHandler()
    fwd.register_handler("dicom", handler)

    study_a = spool.receive("1.2.3.1")
    study_b = spool.receive("1.2.3.2")
    spool.enqueue(study_a, [target_hub])
    spool.enqueue(study_b, [target_hub])

    count = fwd.process_once(limit=1)  # claims study_a's route, fails, retries

    assert count == 1
    # The retry must have gone back to study_a, not skipped to study_b.
    assert attempts[0].study_id == study_a
    assert attempts[-1].study_id == study_a
    assert spool.state(study_a) == StudyState.SENT
    # study_b must be untouched and still waiting
    assert spool.state(study_b) == StudyState.QUEUED


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


# ── Slice 4b (review F2): handlers dispatch per destination, not per type ─


def test_multiple_dicom_destinations_each_get_own_handler(
    target_hub: DICOMDestination, target_pacs: DICOMDestination
) -> None:
    """Two enabled ``dicom`` destinations must each deliver via their own
    handler (registered per destination name) — not collapse onto whichever
    handler was registered last (review F2 misdelivery bug)."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=1))

    hub_handler = FakeHandler(succeed=True)
    pacs_handler = FakeHandler(succeed=True)
    fwd.register_handler("dicom", hub_handler, target_name="hub")
    fwd.register_handler("dicom", pacs_handler, target_name="pacs")

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, target_pacs])

    fwd.process_once(limit=4)

    assert spool.state(study_id) == StudyState.SENT
    assert [t.study_id for t in hub_handler.calls] == [study_id]
    assert [t.study_id for t in pacs_handler.calls] == [study_id]


def test_unnamed_registration_keeps_type_fallback(
    target_hub: DICOMDestination,
) -> None:
    """A registration without ``target_name`` keeps working as the type-level
    fallback (back-compat for existing callers/tests)."""
    forwarder = Forwarder(
        default_config(), Spool(mem_database()), retry=RetryPolicy(base_delay_sec=0, max_attempts=1)
    )
    handler = FakeHandler(succeed=True)
    forwarder.register_handler("dicom", handler)

    study_id = forwarder.spool.receive("1.2.3.4")
    forwarder.spool.enqueue(study_id, [target_hub])

    forwarder.process_once()

    assert forwarder.spool.state(study_id) == StudyState.SENT
    assert len(handler.calls) == 1


def test_destination_registration_wins_over_type_fallback(
    target_hub: DICOMDestination, target_pacs: DICOMDestination
) -> None:
    """When both a type fallback and a per-destination handler exist for a
    target, the per-destination handler is used."""
    spool = Spool(mem_database())
    fwd = Forwarder(default_config(), spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=1))

    fallback = FakeHandler(succeed=True)
    specific = FakeHandler(succeed=True)
    fwd.register_handler("dicom", fallback)  # type-level
    fwd.register_handler("dicom", specific, target_name="pacs")  # destination-level

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, target_pacs])

    fwd.process_once(limit=4)

    assert spool.state(study_id) == StudyState.SENT
    assert len(fallback.calls) == 1  # only 'hub' (no specific handler)
    assert [t.target_name for t in fallback.calls] == ["hub"]
    assert [t.target_name for t in specific.calls] == ["pacs"]


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


# ── Slice 6 (S03-T2): forwarding emits audit events ────────────────────


def _forwarder_with_audit(spool: Spool, handler: FakeHandler) -> Forwarder:
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.forwarder import Forwarder, RetryPolicy

    cfg = default_config()
    fwd = Forwarder(
        cfg, spool, retry=RetryPolicy(base_delay_sec=0, max_attempts=1), audit=AuditLog(spool._db)
    )
    fwd.register_handler("dicom", handler)
    return fwd


def test_successful_delivery_emits_audit_events(
    target_hub: DICOMDestination,
) -> None:
    """A delivered task records FORWARD_START and FORWARD_COMPLETE, and the
    chained hash stays intact (K5 coverage for the forwarding path)."""
    spool = Spool(mem_database())
    fwd = _forwarder_with_audit(spool, FakeHandler(succeed=True))

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    fwd.process_once()

    from mercure_gateway.audit import AuditLog

    events = [e.event for e in AuditLog(spool._db).list_events()]
    assert "FORWARD_START" in events
    assert "FORWARD_COMPLETE" in events
    assert "FORWARD_ERROR" not in events
    ok, errors = AuditLog(spool._db).verify()
    assert ok is True
    assert errors == []


def test_failed_delivery_emits_forward_error(
    target_hub: DICOMDestination,
) -> None:
    """A failed (retry-exhausted) task records FORWARD_ERROR with the error."""
    spool = Spool(mem_database())
    fwd = _forwarder_with_audit(spool, FakeHandler(succeed=False))

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    fwd.process_once()

    from mercure_gateway.audit import AuditLog

    events = [e.event for e in AuditLog(spool._db).list_events()]
    assert "FORWARD_START" in events
    assert "FORWARD_COMPLETE" not in events
    assert "FORWARD_ERROR" in events
    # The error detail must carry the target and study for operators.
    err = next(e for e in AuditLog(spool._db).list_events() if e.event == "FORWARD_ERROR")
    assert err.detail["target_name"] == "hub"
    assert err.detail["study_uid"] == "1.2.3.4"
    ok, _ = AuditLog(spool._db).verify()
    assert ok is True


def test_forward_audit_is_not_emitted_without_audit(
    forwarder: Forwarder, target_hub: DICOMDestination
) -> None:
    """A Forwarder built without an AuditLog (legacy) records no audit rows —
    existing callers keep working unchanged."""
    study_id = forwarder.spool.receive("1.2.3.4")
    forwarder.spool.enqueue(study_id, [target_hub])

    count = forwarder.process_once()

    from mercure_gateway.audit import AuditLog

    assert count == 1
    assert AuditLog(forwarder.spool._db).list_events() == []


# ── Slice 7 (S03-T4): manual re-forward / retry service ───────────────


def test_reforward_study_requeues_failed_and_resets_attempts(
    target_hub: DICOMDestination, target_pacs: DICOMDestination
) -> None:
    """A FAILED study is re-queued by ``reforward_study`` with its attempt
    counter reset; already-complete routes are untouched."""
    spool = Spool(mem_database())
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, target_pacs])  # two routes

    # Simulate FAILED state: fail the first route, then flag the study.
    routes = spool._db.get_routes(study_id)
    spool._db.mark_route_error(routes[0]["id"], "connection refused")
    spool._db.mark_route_error(routes[1]["id"], "connection refused")
    spool._db.set_study_state(study_id, StudyState.FAILED.value)
    assert spool.state(study_id) == StudyState.FAILED

    # Complete one route so it must NOT be re-queued.
    spool._db.mark_route_sent(spool._db.get_routes(study_id)[0]["id"])

    requeued = spool.reforward_study(study_id)

    assert requeued == 1  # only the non-complete route
    assert spool.state(study_id) == StudyState.QUEUED
    routes = spool._db.get_routes(study_id)
    assert [r["status"] for r in routes] == ["complete", "waiting"]
    assert all(int(r["attempts"]) == 0 for r in routes)


def test_reforward_study_emits_retry_manual_audit(
    target_hub: DICOMDestination,
) -> None:
    """Manual re-forward records a RETRY_MANUAL audit event so operators can
    prove who/what triggered a retry (US-04 / K5)."""
    from mercure_gateway.audit import AuditLog

    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.fail(study_id, "hub", "boom", max_attempts=1)

    spool.reforward_study(study_id)

    events = AuditLog(db).list_events()
    assert events[0].event == "RETRY_MANUAL"
    assert events[0].detail["study_uid"] == "1.2.3.4"
    ok, _ = AuditLog(db).verify()
    assert ok is True


def test_failed_study_reforward_then_sent(
    target_hub: DICOMDestination,
) -> None:
    """Full US-04 lifecycle: FAILED → manual re-forward → SENDING → SENT once
    the handler succeeds on the retry."""
    from mercure_gateway.audit import AuditLog
    from mercure_gateway.forwarder import RetryPolicy

    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])

    # First pass fails (handler fails once) and exhausts the budget.
    failing = FakeHandler(succeed=False)
    fwd = Forwarder(
        default_config(),
        spool,
        retry=RetryPolicy(base_delay_sec=0, max_attempts=1),
        audit=AuditLog(db),
    )
    fwd.register_handler("dicom", failing)
    fwd.process_once()
    assert spool.state(study_id) == StudyState.FAILED

    # Manual re-forward.
    spool.reforward_study(study_id)
    assert spool.state(study_id) == StudyState.QUEUED

    # Retry succeeds → SENDING → SENT.
    ok_handler = FakeHandler(succeed=True)
    fwd2 = Forwarder(
        default_config(),
        spool,
        retry=RetryPolicy(base_delay_sec=0, max_attempts=1),
        audit=AuditLog(db),
    )
    fwd2.register_handler("dicom", ok_handler)
    fwd2.process_once()

    assert spool.state(study_id) == StudyState.SENT
    assert ok_handler.calls  # handler actually dispatched
    events = [e.event for e in AuditLog(db).list_events()]
    assert "RETRY_MANUAL" in events
    assert "FORWARD_COMPLETE" in events