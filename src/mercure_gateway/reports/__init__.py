"""Report retrieval (PRD §5.2 step 5, §2.3, Sprint 05).

Retrieves study reports from a PACS query source (DICOM SR via C-FIND/C-MOVE
in the MVP; pluggable transports — DICOMweb QIDO/WADO, HL7/FHIR — in v1.1).
Report status lifecycle: PENDING → RETRIEVING → RETRIEVED / FAILED.

The transports are injected as ``finder`` (C-FIND SCU) and ``mover`` (C-MOVE
SCU) callables so the state machine can be unit-tested against fakes; the
production wiring in ``main.py`` supplies the real :class:`ReportFinder` and
:class:`ReportRetrieve` implementations.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from mercure_gateway.audit import AuditLog
from mercure_gateway.audit.events import (
    REPORT_REQUESTED,
    REPORT_RETRIEVAL_FAILED,
    REPORT_RETRIEVED,
    REPORT_RETRIEVING,
    REPORT_SLA_EXPIRED,
)
from mercure_gateway.config import ReportConfig
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import Database

__all__ = ["ReportRetriever", "ReportStatus"]

logger = logging.getLogger(__name__)

# Finder/mover callable signatures (see reports.find / reports.move).
Finder = Callable[..., list[Any]]
Mover = Callable[..., list[Any]]


class ReportStatus:
    """Report retrieval lifecycle states (PRD §3.3 / §5.4)."""

    PENDING = "pending"
    RETRIEVING = "retrieving"
    RETRIEVED = "retrieved"
    FAILED = "failed"


# Legal transitions: current → allowed next states.
_TRANSITIONS = {
    ReportStatus.PENDING: {ReportStatus.RETRIEVING},
    ReportStatus.RETRIEVING: {ReportStatus.RETRIEVED, ReportStatus.FAILED},
    ReportStatus.RETRIEVED: set(),
    ReportStatus.FAILED: {ReportStatus.RETRIEVING},  # re-request after failure
}


class ReportRetriever:
    """Polls for studies needing reports and retrieves them from the query source."""

    def __init__(
        self,
        config: ReportConfig,
        database: Database,
        spool: Spool,
        *,
        audit: AuditLog | None = None,
        clock: Any | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.spool = spool
        self.audit = audit
        self.finder: Finder | None = None
        self.mover: Mover | None = None
        self._clock: Any = clock or time
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # report_id → wall-clock time when requested (SLA tracking, K3).
        self._requested_at: dict[int, float] = {}

    def start(self) -> None:
        """Begin the polling loop (every ``config.poll_interval_sec``)."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="report-poller")
        self._thread.start()
        logger.info("Report retriever started (poll every %ds)", self.config.poll_interval_sec)

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        """Whether the retrieval loop is active."""
        return self._running

    def _poll_loop(self) -> None:
        """Background loop: poll once, then sleep for the configured interval."""
        while self._running:
            try:
                self._poll_once()
            except Exception:
                logger.exception("report poll cycle failed")
            self._stop_event.wait(self.config.poll_interval_sec)

    def _loop_iteration(self) -> None:
        """One poll + sleep cycle (testable without a thread)."""
        self._poll_once()
        self._clock.sleep(self.config.poll_interval_sec)

    def _poll_once(self) -> None:
        """Retrieve every PENDING report and flag any that exceeded the SLA."""
        for row in self.database.list_reports(status=ReportStatus.PENDING):
            self._check_sla(row)
            try:
                self.retrieve(int(row["id"]))
            except RuntimeError as exc:
                logger.warning("report %s skipped: %s", row["id"], exc)

    def _check_sla(self, row: Any) -> None:
        """Emit REPORT_SLA_EXPIRED when a PENDING report exceeds the SLA window."""
        report_id = int(row["id"])
        requested = self._requested_at.get(report_id)
        if requested is None:
            return
        elapsed = self._clock.time() - requested
        if elapsed > self.config.sla_seconds:
            self._emit(
                REPORT_SLA_EXPIRED,
                {
                    "report_id": report_id,
                    "study_uid": str(row["study_uid"]),
                    "sla_seconds": self.config.sla_seconds,
                    "elapsed_seconds": round(elapsed, 2),
                },
            )

    def request_report(
        self,
        study_uid: str,
        accession: str | None = None,
        report_type: str = "sr",
        *,
        retrieve_now: bool = False,
    ) -> int:
        """Create a PENDING report row for a study and return its id (US-06).

        ``report_type`` is ``"sr"``, ``"pdf"`` or ``"both"`` (creates two
        PENDING rows and returns the id of the first).  When ``retrieve_now``
        is set the report is retrieved immediately, bypassing the poll wait.
        """
        study = self.database.get_study_by_uid(study_uid)
        if study is None:
            raise KeyError(f"unknown study {study_uid!r}")
        types = ["sr", "pdf"] if report_type == "both" else [report_type]
        report_id: int | None = None
        for rtype in types:
            report_id = self.database.insert_report(
                study_id=study["id"],
                study_uid=study_uid,
                report_type=rtype,
                accession=accession,
                status=ReportStatus.PENDING,
            )
            self._requested_at[report_id] = self._clock.time()
            self._emit(
                REPORT_REQUESTED,
                {
                    "report_id": report_id,
                    "study_uid": study_uid,
                    "accession": accession,
                    "report_type": rtype,
                },
            )
            if retrieve_now:
                self.retrieve(report_id)
        assert report_id is not None
        return report_id

    def retrieve(self, report_id: int) -> str:
        """Walk one report through PENDING → RETRIEVING → RETRIEVED / FAILED.

        Returns the final status.  Raises ``RuntimeError`` on an illegal
        transition (e.g. a RETRIEVED report).  The finder/mover transports must
        be injected (via ``self.finder`` / ``self.mover``) before calling.
        """
        row = self.database.get_report(report_id)
        if row is None:
            raise KeyError(f"unknown report {report_id!r}")
        current = row["status"]
        if ReportStatus.RETRIEVING not in _TRANSITIONS.get(current, set()):
            raise RuntimeError(
                f"illegal transition: report {report_id} is already {current!r}"
            )

        self._transition(report_id, ReportStatus.RETRIEVING)
        self._emit(
            REPORT_RETRIEVING,
            {"report_id": report_id, "study_uid": str(row["study_uid"])},
        )
        try:
            self._do_retrieve(report_id, row)
            return ReportStatus.RETRIEVED
        except Exception as exc:  # noqa: BLE001 — boundary: mark report failed
            logger.exception("report %d retrieval failed", report_id)
            self._transition(report_id, ReportStatus.FAILED)
            self._emit(
                REPORT_RETRIEVAL_FAILED,
                {
                    "report_id": report_id,
                    "study_uid": str(row["study_uid"]),
                    "error": str(exc),
                },
            )
            return ReportStatus.FAILED

    def _do_retrieve(self, report_id: int, row: Any) -> None:
        """C-FIND then C-MOVE the requested report type; persist on success."""
        if self.finder is None or self.mover is None:
            raise RuntimeError("report transports (finder/mover) not configured")

        report_type = str(row["report_type"])
        matches = self.finder(
            study_uid=str(row["study_uid"]),
            accession=row["accession"],
            report_types=[report_type],
        )
        if not matches:
            raise RuntimeError("no matching report on PACS")
        retrieved = self.mover(matches)
        if not retrieved:
            raise RuntimeError("C-MOVE returned no instances")

        first = retrieved[0]
        self.database.set_report_status(
            report_id,
            ReportStatus.RETRIEVED,
            file_path=str(first.file_path),
            sop_class_uid=first.sop_class_uid,
        )
        self._emit(
            REPORT_RETRIEVED,
            {
                "report_id": report_id,
                "study_uid": str(row["study_uid"]),
                "report_type": report_type,
                "file_path": str(first.file_path),
                "sop_class_uid": first.sop_class_uid,
            },
        )

    def _transition(self, report_id: int, status: str) -> None:
        """Persist a status change."""
        self.database.set_report_status(report_id, status)

    def _emit(self, event: str, detail: dict[str, Any]) -> None:
        """Append an audit event when an AuditLog is wired; otherwise no-op."""
        if self.audit is not None:
            self.audit.append(event, detail)
