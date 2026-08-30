"""S04-T6 (RED): operator console v0 service layer (§2.2 Flow B, refinement §7).

The console is a read-only dashboard (queue/status/logs/errors) served by the
FastAPI core over localhost:8080.  The UI itself is smoke-tested manually; the
**service layer** — the module that aggregates the dashboard data — is what gets
RED tests here.

``ConsoleService.dashboard()`` returns one structured summary combining:
  - queue statistics (studies by state)
  - recent audit events (operations log view)
  - recent error events (FORWARD_ERROR / STUDY_FAILED)
  - audit chain head hash (tamper-evidence)
  - tail of the rotating text log (when present)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import DICOMDestination
from mercure_gateway.spool import Spool, StudyState
from mercure_gateway.spool.db import mem_database
from mercure_gateway.web.console import ConsoleService


@pytest.fixture()
def target_hub() -> DICOMDestination:
    return DICOMDestination(
        name="hub", type="dicom", host="hub.local", port=11112, aet_target="MERCURE"
    )


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


def test_empty_dashboard_shape(spool: Spool) -> None:
    service = ConsoleService(spool, AuditLog(spool._db))
    dash = service.dashboard()
    assert dash.queue["total"] == 0
    assert dash.recent_events == []
    assert dash.recent_errors == []
    assert len(dash.head_hash) == 64
    assert dash.text_log_tail == []


def test_dashboard_reports_queue_counts(spool: Spool, target_hub: DICOMDestination) -> None:
    s1 = spool.receive("1.1.1")
    spool.enqueue(s1, [target_hub])
    spool.receive("2.2.2")
    spool._db.set_study_state(s1, StudyState.SENDING.value)

    dash = ConsoleService(spool, AuditLog(spool._db)).dashboard()

    assert dash.queue["total"] == 2
    assert dash.queue["sending"] == 1


def test_dashboard_lists_recent_audit_events(spool: Spool) -> None:
    audit = AuditLog(spool._db)
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3"})
    audit.append("FORWARD_COMPLETE", {"study_uid": "1.2.3"})

    dash = ConsoleService(spool, audit).dashboard()

    events = [e["event"] for e in dash.recent_events]
    assert events == ["FORWARD_COMPLETE", "STUDY_RECEIVED"]  # newest first


def test_dashboard_lists_error_events(spool: Spool) -> None:
    audit = AuditLog(spool._db)
    audit.append("FORWARD_ERROR", {"study_uid": "1.2.3", "error": "connection refused"})
    audit.append("STUDY_FAILED", {"study_uid": "1.2.3"})
    audit.append("STUDY_RECEIVED", {"study_uid": "1.2.3"})

    dash = ConsoleService(spool, audit).dashboard()

    err_events = [e["event"] for e in dash.recent_errors]
    assert err_events == ["STUDY_FAILED", "FORWARD_ERROR"]


def test_dashboard_reports_failed_study_count(
    spool: Spool, target_hub: DICOMDestination
) -> None:
    s1 = spool.receive("1.1.1")
    spool.enqueue(s1, [target_hub])
    spool.claim_next(limit=1)
    spool.fail(s1, "hub", "boom", max_attempts=1)
    assert spool.state(s1) == StudyState.FAILED

    dash = ConsoleService(spool, AuditLog(spool._db)).dashboard()

    assert dash.queue["failed"] == 1


def test_dashboard_includes_text_log_tail(tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.textlog import TextLog

    log = TextLog(tmp_path / "operations.log")
    log.info("receiver started")
    log.error("forward failed: connection refused")

    dash = ConsoleService(
        spool, AuditLog(spool._db), text_log_path=tmp_path / "operations.log"
    ).dashboard()

    assert len(dash.text_log_tail) == 2
    assert any("forward failed" in line for line in dash.text_log_tail)


def test_dashboard_limits_text_log_tail(tmp_path: Path, spool: Spool) -> None:
    from mercure_gateway.textlog import TextLog

    log = TextLog(tmp_path / "operations.log")
    for i in range(30):
        log.info(f"line {i}")

    dash = ConsoleService(
        spool, AuditLog(spool._db), text_log_path=tmp_path / "operations.log", tail_lines=10
    ).dashboard()

    assert len(dash.text_log_tail) == 10


def test_dashboard_recent_events_limit(spool: Spool) -> None:
    audit = AuditLog(spool._db)
    for i in range(25):
        audit.append("EVENT", {"n": i})

    dash = ConsoleService(spool, audit, recent_events=10).dashboard()

    assert len(dash.recent_events) == 10
