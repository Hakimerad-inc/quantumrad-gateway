"""S05-T4 (RED): Polling scheduler for report retrieval (K3, §5.5).

``ReportRetriever._poll_once()`` scans PENDING reports and calls
``retrieve()`` on each one.  The thread loop honours ``poll_interval_sec``
and uses an injectable clock for testability.  SLA expiry (>5 min pending)
is flagged via an audit event.
"""

from __future__ import annotations

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import ReportConfig, ReportQuerySource
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


class _InjectedClock:
    """Simulated wall clock for tests — no real sleeping."""

    def __init__(self) -> None:
        self._now = 0.0

    def time(self) -> float:
        return self._now

    def sleep(self, sec: float) -> None:
        self._now += sec

    def advance(self, seconds: float = 0.0) -> None:
        self._now += seconds


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


def test_poll_once_retrieves_pending_reports(spool: Spool) -> None:
    from mercure_gateway.reports import ReportRetriever

    config = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(type="dicom", host="pacs.local", port=104, aet="PACS"),
    )
    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    retriever.finder = lambda **kw: []
    retriever.mover = None
    retriever._clock = _InjectedClock()

    spool.receive("1.2.3.4", accession="ACC-001")
    spool.receive("2.2.2.2")
    r1 = retriever.request_report("1.2.3.4")
    r2 = retriever.request_report("2.2.2.2")

    retriever._poll_once()

    assert spool._db.get_report(r1)["status"] == "failed"
    assert spool._db.get_report(r2)["status"] == "failed"


def test_poll_once_skips_non_pending(spool: Spool) -> None:
    """Only PENDING reports are polled; RETRIEVED ones are skipped."""
    from mercure_gateway.reports import ReportRetriever

    config = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(type="dicom", host="pacs.local", port=104, aet="PACS"),
    )
    retriever = ReportRetriever(config, spool._db, spool)
    spool.receive("1.2.3.4", accession="ACC-001")
    report_id = spool._db.insert_report(
        study_id=1, study_uid="1.2.3.4", report_type="sr", status="retrieved"
    )

    retriever._poll_once()  # should not crash and not touch the report that's already retrieved

    assert spool._db.get_report(report_id)["status"] == "retrieved"


def test_interval_honoured(spool: Spool) -> None:
    """_poll_loop sleeps for poll_interval_sec between polls."""
    from mercure_gateway.reports import ReportRetriever

    config = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(type="dicom", host="pacs.local", port=104, aet="PACS"),
        poll_interval_sec=30,
    )
    retriever = ReportRetriever(config, spool._db, spool)
    clock = _InjectedClock()
    retriever._clock = clock

    clock.advance(seconds=10)
    retriever._loop_iteration()

    assert clock.time() == 40.0  # 10 + 30 sleep


def test_sla_expiry_emits_event(spool: Spool) -> None:
    """A PENDING report that exceeds sla_seconds triggers an audit event."""
    from mercure_gateway.reports import ReportRetriever

    config = ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(type="dicom", host="pacs.local", port=104, aet="PACS"),
        sla_seconds=120,
    )
    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    retriever.finder = lambda **kw: []
    clock = _InjectedClock()
    retriever._clock = clock

    spool.receive("1.2.3.4", accession="ACC-001")
    retriever.request_report("1.2.3.4")

    clock.advance(seconds=150)  # past SLA window
    retriever._poll_once()

    events = [e.event for e in AuditLog(spool._db).list_events()]
    assert "REPORT_SLA_EXPIRED" in events
