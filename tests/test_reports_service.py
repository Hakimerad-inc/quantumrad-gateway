"""S05-T3 (RED): Report status machine wiring (US-05, §3.3, §5.2-6).

``ReportRetriever`` walks the report lifecycle: PENDING → RETRIEVING →
RETRIEVED / FAILED.  Each transition emits an audit event.  Illegal
transitions (e.g. RETRIEVED → RETRIEVING) are rejected.
"""

from __future__ import annotations

import pytest

from mercure_gateway.audit import AuditLog
from mercure_gateway.config import ReportConfig, ReportQuerySource
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


@pytest.fixture()
def spool() -> Spool:
    return Spool(mem_database())


@pytest.fixture()
def config() -> ReportConfig:
    return ReportConfig(
        enabled=True,
        query_source=ReportQuerySource(type="dicom", host="pacs.local", port=104, aet="PACS"),
    )


def test_request_report_creates_pending_row(spool: Spool, config: ReportConfig) -> None:
    from mercure_gateway.reports import ReportRetriever

    retriever = ReportRetriever(config, spool._db, spool)
    spool.receive("1.2.3.4", accession="ACC-001", modality="CT")

    report_id = retriever.request_report("1.2.3.4", accession="ACC-001", report_type="sr")

    row = spool._db.get_report(report_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["report_type"] == "sr"
    assert row["study_uid"] == "1.2.3.4"


def test_retrieve_transition_walk(spool: Spool, config: ReportConfig) -> None:
    """Full walk: PENDING → RETRIEVING → RETRIEVED with a fake finder+mover."""
    from pathlib import Path

    from mercure_gateway.reports import ReportRetriever, ReportStatus

    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    spool.receive("1.2.3.4", accession="ACC-001")
    report_id = retriever.request_report("1.2.3.4", "ACC-001", "sr")

    # Fake finder that returns one match
    def _fake_find(**kw):
        return [
            pytest.importorskip("mercure_gateway.reports.find").ReportMatch(
                sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
                study_uid="1.2.3.4",
                series_uid="1.2.3.4.1",
                sop_instance_uid="1.2.3.4.5.6.7",
            )
        ]

    # Fake mover that returns one retrieved report
    def _fake_move(matches):
        return [
            pytest.importorskip("mercure_gateway.reports.move").RetrievedReport(
                sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
                study_uid="1.2.3.4",
                sop_instance_uid="1.2.3.4.5.6.7",
                file_path=Path("/tmp/reports/sr/1.2.3.4.5.6.7.dcm"),
            )
        ]

    retriever.finder = _fake_find
    retriever.mover = _fake_move

    final_status = retriever.retrieve(report_id)

    assert final_status == ReportStatus.RETRIEVED
    row = spool._db.get_report(report_id)
    assert row["status"] == "retrieved"
    assert row["file_path"] is not None
    assert row["sop_class_uid"] is not None


def test_retrieval_failure(spool: Spool, config: ReportConfig) -> None:
    """When finder raises, the report transitions to FAILED."""
    from mercure_gateway.reports import ReportRetriever, ReportStatus

    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    spool.receive("1.2.3.4", accession="ACC-001")
    report_id = retriever.request_report("1.2.3.4", "ACC-001", "sr")

    # Finder that raises
    def _fail(**kw):
        raise RuntimeError("PACS unreachable")

    retriever.finder = _fail
    retriever.mover = None

    final_status = retriever.retrieve(report_id)

    assert final_status == ReportStatus.FAILED
    row = spool._db.get_report(report_id)
    assert row["status"] == "failed"


def test_illegal_transition_rejected(spool: Spool, config: ReportConfig) -> None:
    """Calling retrieve on a RETRIEVED report raises."""
    from mercure_gateway.reports import ReportRetriever

    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    spool.receive("1.2.3.4", accession="ACC-001")
    report_id = spool._db.insert_report(
        study_id=1, study_uid="1.2.3.4", report_type="sr", status="retrieved",
        file_path="/tmp/report.dcm",
    )

    with pytest.raises(RuntimeError, match="illegal transition"):
        retriever.retrieve(report_id)


def test_audit_events_emitted(spool: Spool, config: ReportConfig) -> None:
    """Each transition produces an audit event."""
    from mercure_gateway.reports import ReportRetriever

    db = spool._db
    audit = AuditLog(db)
    retriever = ReportRetriever(config, db, spool, audit=audit)
    spool.receive("1.2.3.4", accession="ACC-001")
    report_id = retriever.request_report("1.2.3.4", "ACC-001", "sr")

    # Finder that returns a match so it transitions to RETRIEVING
    def _find(**kw):
        return []
    retriever.finder = _find
    retriever.mover = None

    retriever.retrieve(report_id)

    events = [e.event for e in audit.list_events()]
    assert "REPORT_REQUESTED" in events
    assert "REPORT_RETRIEVING" in events
    assert "REPORT_RETRIEVAL_FAILED" in events or "REPORT_RETRIEVED" in events


# ── S05-T5: on-demand "Request report" (US-06) ──────────────────────────


def test_request_report_immediate_retrieval(spool: Spool, config: ReportConfig) -> None:
    """Manual trigger retrieves immediately (bypasses poll wait)."""
    from mercure_gateway.reports import ReportRetriever

    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    spool.receive("1.2.3.4", accession="ACC-001")
    report_id = retriever.request_report("1.2.3.4", "ACC-001", "sr", retrieve_now=True)

    assert spool._db.get_report(report_id)["status"] == "failed"


def test_request_report_immediate_success(spool: Spool, config: ReportConfig) -> None:
    """Manual request with a working finder/mover lands RETRIEVED immediately."""
    from pathlib import Path

    from mercure_gateway.reports import ReportRetriever, ReportStatus

    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    spool.receive("1.2.3.4", accession="ACC-001")

    def _find(**kw):
        return [
            pytest.importorskip("mercure_gateway.reports.find").ReportMatch(
                sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
                study_uid="1.2.3.4",
                series_uid="1.2.3.4.1",
                sop_instance_uid="1.2.3.4.5.6.7",
            )
        ]

    def _move(matches):
        return [
            pytest.importorskip("mercure_gateway.reports.move").RetrievedReport(
                sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
                study_uid="1.2.3.4",
                sop_instance_uid="1.2.3.4.5.6.7",
                file_path=Path("/tmp/reports/sr/1.2.3.4.5.6.7.dcm"),
            )
        ]

    retriever.finder = _find
    retriever.mover = _move

    report_id = retriever.request_report("1.2.3.4", "ACC-001", "sr", retrieve_now=True)

    assert spool._db.get_report(report_id)["status"] == ReportStatus.RETRIEVED


def test_rerequest_after_failure_succeeds(spool: Spool, config: ReportConfig) -> None:
    """A failed request can be re-requested and reach RETRIEVED."""
    from mercure_gateway.reports import ReportRetriever

    retriever = ReportRetriever(config, spool._db, spool, audit=AuditLog(spool._db))
    spool.receive("1.2.3.4", accession="ACC-001")

    # First attempt fails (empty finder)
    def _empty(**kw):
        return []
    retriever.finder = _empty
    report_id = retriever.request_report("1.2.3.4", "ACC-001", "sr", retrieve_now=True)
    assert spool._db.get_report(report_id)["status"] == "failed"

    # Re-request after failure succeeds
    def _find(**kw):
        return [
            pytest.importorskip("mercure_gateway.reports.find").ReportMatch(
                sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
                study_uid="1.2.3.4",
                series_uid="1.2.3.4.1",
                sop_instance_uid="1.2.3.4.5.6.7",
            )
        ]

    def _move(matches):
        return [
            pytest.importorskip("mercure_gateway.reports.move").RetrievedReport(
                sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
                study_uid="1.2.3.4",
                sop_instance_uid="1.2.3.4.5.6.7",
                file_path="/tmp/reports/sr/1.2.3.4.5.6.7.dcm",
            )
        ]

    retriever.finder = _find
    retriever.mover = _move
    result = retriever.retrieve(report_id)

    assert result == "retrieved"
    assert spool._db.get_report(report_id)["status"] == "retrieved"


def test_request_report_type_filter(spool: Spool, config: ReportConfig) -> None:
    """The requested report type is recorded on the PENDING row (US-06)."""
    from mercure_gateway.reports import ReportRetriever

    retriever = ReportRetriever(config, spool._db, spool)
    spool.receive("1.2.3.4", accession="ACC-001")

    sr_id = retriever.request_report("1.2.3.4", "ACC-001", report_type="sr")
    pdf_id = retriever.request_report("1.2.3.4", "ACC-001", report_type="pdf")

    assert spool._db.get_report(sr_id)["report_type"] == "sr"
    assert spool._db.get_report(pdf_id)["report_type"] == "pdf"


def test_request_report_both_types(spool: Spool, config: ReportConfig) -> None:
    """Requesting 'both' creates SR and PDF PENDING rows."""
    from mercure_gateway.reports import ReportRetriever

    retriever = ReportRetriever(config, spool._db, spool)
    spool.receive("1.2.3.4", accession="ACC-001")

    retriever.request_report("1.2.3.4", "ACC-001", report_type="both")

    rows = spool._db.list_reports(study_uid="1.2.3.4")
    types = sorted(r["report_type"] for r in rows)
    assert types == ["pdf", "sr"]