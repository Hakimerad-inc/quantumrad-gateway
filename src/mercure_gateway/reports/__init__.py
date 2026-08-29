"""Report retrieval (PRD §5.2 step 5, §2.3).

Retrieves study reports from a PACS query source (DICOM SR via C-FIND/C-MOVE
in the MVP; pluggable transports — DICOMweb QIDO/WADO, HL7/FHIR — in v1.1).
Report status lifecycle: PENDING → RETRIEVING → RETRIEVED / RETRIEVAL_FAILED.

Skeleton only: query/retrieve transports are not wired yet.
"""

from __future__ import annotations

from mercure_gateway.config import ReportConfig
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import Database

__all__ = ["ReportRetriever", "ReportStatus"]


class ReportStatus:
    """Report retrieval lifecycle states (PRD §3.3 / §5.4)."""

    PENDING = "pending"
    RETRIEVING = "retrieving"
    RETRIEVED = "retrieved"
    FAILED = "failed"


class ReportRetriever:
    """Polls for studies needing reports and retrieves them from the query source."""

    def __init__(self, config: ReportConfig, database: Database, spool: Spool) -> None:
        self.config = config
        self.database = database
        self.spool = spool
        self._running = False

    def start(self) -> None:
        """Begin the polling loop (every ``config.poll_interval_sec``)."""
        self._running = True

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the retrieval loop is active."""
        return self._running

    def request_report(self, study_uid: str, accession: str | None = None) -> int:
        """Create a PENDING report row for a study and return its id."""
        study = self.database.get_study_by_uid(study_uid)
        if study is None:
            raise KeyError(f"unknown study {study_uid!r}")
        return self.database.insert_report(
            study_id=study["id"],
            study_uid=study_uid,
            report_type="SR",
            accession=accession,
            status=ReportStatus.PENDING,
        )

    def retrieve(self, report_id: int) -> None:
        """Issue C-FIND/C-MOVE to the query source and store the returned SR.

        Updates the report row to RETRIEVED (with file path) or FAILED.
        Transport is not implemented in the scaffold.
        """
        raise NotImplementedError
