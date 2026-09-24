"""S04-T1 (RED): audit event coverage sweep (PRD §5.4, K5).

Every study lifecycle transition must produce ≥1 audit event, so operators can
reconstruct the full path of a study from the audit log alone.  This walks a
study through every state and asserts each transition emits its event.

Event vocabulary (see ``mercure_gateway.audit.events``):
  STUDY_RECEIVED  — instance/study persisted by the receiver (store-before-ack)
  STUDY_QUEUED    — study scheduled for delivery
  FORWARD_START   — worker claimed the task and began delivery
  FORWARD_COMPLETE— delivery to one destination succeeded
  FORWARD_ERROR   — delivery to one destination failed
  STUDY_SENT      — every destination delivered (study → SENT)
  STUDY_FAILED    — retry budget exhausted (study → FAILED)
  RETRY_MANUAL    — operator re-forwarded a study
"""

from __future__ import annotations

from pathlib import Path

from mercure_gateway.audit import AuditLog
from mercure_gateway.audit.events import (
    ALL_EVENTS,
    AUDIT_ANCHOR_FAILED,
    AUDIT_CHAIN_FAILED,
    FORWARD_COMPLETE,
    FORWARD_ERROR,
    FORWARD_START,
    PRUNE_AUDIT,
    REPORT_REQUESTED,
    REPORT_RETRIEVAL_FAILED,
    REPORT_RETRIEVED,
    REPORT_RETRIEVING,
    REPORT_SLA_EXPIRED,
    RETRY_MANUAL,
    STUDY_FAILED,
    STUDY_QUEUED,
    STUDY_RECEIVED,
    STUDY_SENT,
)
from mercure_gateway.config import DICOMDestination, default_config
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


def _events(db) -> list[str]:
    return [e.event for e in AuditLog(db).list_events()]


def test_happy_path_every_transition_is_audited(target_hub: DICOMDestination) -> None:
    """A study walked RECEIVED→QUEUED→SENDING→SENT emits one event per transition."""
    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))
    fwd = Forwarder(
        default_config(),
        spool,
        retry=RetryPolicy(base_delay_sec=0, max_attempts=1),
        audit=AuditLog(db),
    )
    fwd.register_handler("dicom", FakeHandler(succeed=True))

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.SENT
    events = _events(db)
    assert "STUDY_RECEIVED" in events
    assert "STUDY_QUEUED" in events
    assert "FORWARD_START" in events
    assert "FORWARD_COMPLETE" in events
    assert "STUDY_SENT" in events
    ok, errors = AuditLog(db).verify()
    assert ok is True
    assert errors == []


def test_failure_path_every_transition_is_audited(target_hub: DICOMDestination) -> None:
    """RECEIVED→QUEUED→SENDING→FAILED emits FORWARD_ERROR + STUDY_FAILED."""
    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))
    fwd = Forwarder(
        default_config(),
        spool,
        retry=RetryPolicy(base_delay_sec=0, max_attempts=1),
        audit=AuditLog(db),
    )
    fwd.register_handler("dicom", FakeHandler(succeed=False))

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    fwd.process_once()

    assert spool.state(study_id) == StudyState.FAILED
    events = _events(db)
    assert "STUDY_RECEIVED" in events
    assert "STUDY_QUEUED" in events
    assert "FORWARD_START" in events
    assert "FORWARD_ERROR" in events
    assert "STUDY_FAILED" in events
    ok, _ = AuditLog(db).verify()
    assert ok is True


def test_manual_reforward_transition_is_audited(target_hub: DICOMDestination) -> None:
    """FAILED → RETRY_MANUAL → QUEUED re-queues the study for delivery."""
    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.claim_next(limit=1)  # bumps route attempts to 1
    spool.fail(study_id, "hub", "boom", max_attempts=1)
    assert spool.state(study_id) == StudyState.FAILED

    requeued = spool.reforward_study(study_id)
    assert requeued == 1
    assert spool.state(study_id) == StudyState.QUEUED
    events = _events(db)
    assert "RETRY_MANUAL" in events


def test_no_audit_log_emits_no_events(target_hub: DICOMDestination) -> None:
    """Spool built without an AuditLog (legacy callers) records nothing."""
    spool = Spool(mem_database())
    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub])
    spool.fail(study_id, "hub", "boom", max_attempts=1)
    assert AuditLog(spool._db).list_events() == []


def test_study_sent_emitted_once_per_study(target_hub: DICOMDestination) -> None:
    """STUDY_SENT fires exactly once — after the LAST destination completes."""
    db = mem_database()
    spool = Spool(db, audit=AuditLog(db))
    fwd = Forwarder(
        default_config(),
        spool,
        retry=RetryPolicy(base_delay_sec=0, max_attempts=1),
        audit=AuditLog(db),
    )
    fwd.register_handler("dicom", FakeHandler(succeed=True))
    second = DICOMDestination(
        name="pacs", type="dicom", host="pacs.local", port=104, aet_target="PACS"
    )

    study_id = spool.receive("1.2.3.4")
    spool.enqueue(study_id, [target_hub, second])
    fwd.process_once(limit=2)

    assert spool.state(study_id) == StudyState.SENT
    sent = [e for e in AuditLog(db).list_events() if e.event == STUDY_SENT]
    assert len(sent) == 1


def test_event_vocabulary_is_consistent() -> None:
    """The vocabulary module lists every event the code emits (K5: no orphan
    event names — a typo'd constant would fail coverage silently)."""
    assert set(ALL_EVENTS) == {
        STUDY_RECEIVED,
        STUDY_QUEUED,
        FORWARD_START,
        FORWARD_COMPLETE,
        FORWARD_ERROR,
        STUDY_SENT,
        STUDY_FAILED,
        RETRY_MANUAL,
        PRUNE_AUDIT,
        AUDIT_CHAIN_FAILED,
        AUDIT_ANCHOR_FAILED,
        REPORT_REQUESTED,
        REPORT_RETRIEVING,
        REPORT_RETRIEVED,
        REPORT_RETRIEVAL_FAILED,
        REPORT_SLA_EXPIRED,
    }


def test_head_anchorer_receives_every_new_head(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """set_head_anchorer: the callback fires after every append with the head."""
    heads: list[str] = []
    audit = AuditLog(mem_database())
    audit.set_head_anchorer(heads.append)

    audit.append("STUDY_RECEIVED")
    audit.append("STUDY_QUEUED")

    assert heads[-1] == audit.head_hash()
    assert len(heads) == 2


def test_anchor_head_to_file_appends_all_heads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """anchor_head_to_file appends one head per line; nothing is rewritten."""
    from mercure_gateway.audit import anchor_head_to_file

    path = tmp_path / "heads.txt"
    anchor = anchor_head_to_file(path)

    audit = AuditLog(mem_database())
    audit.set_head_anchorer(anchor)
    audit.append("STUDY_RECEIVED")
    audit.append("STUDY_QUEUED")
    audit.append("STUDY_SENT")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 3
    assert lines[-1] == audit.head_hash()
    # Historical heads survive (append-only).
    assert len(set(lines)) == 3


def test_failing_head_anchorer_never_breaks_append() -> None:
    """A raising anchorer is isolated: the event is still persisted (US-10)."""
    def boom(_head: str) -> None:
        raise OSError("disk full")

    audit = AuditLog(mem_database())
    audit.set_head_anchorer(boom)

    rowid = audit.append("STUDY_RECEIVED")  # must not raise

    assert audit.list_events()[0].event == "STUDY_RECEIVED"
    assert rowid >= 1
