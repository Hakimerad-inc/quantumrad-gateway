"""Report query transport — C-FIND SCU (US-05, refinement §2.3, S05-T1).

Queries the configured PACS by StudyInstanceUID or AccessionNumber, filtering
by SOP Class UID to distinguish DICOM SR
(``1.2.840.10008.5.1.4.1.1.88.33``) from Encapsulated PDF
(``1.2.840.10008.5.1.4.1.1.104.2``).

The query runs at the **IMAGE** level of the Study Root Query/Retrieve model,
so individual report SOP instances (not whole studies) are returned.  The
``report_types`` filter maps to the requested SOP classes and is also applied
client-side to the returned instances.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE

__all__ = ["ReportFinder", "ReportFinderError", "ReportMatch"]

# SOP classes the report query filters on (refinement §2.3).
SR_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.88.33"
PDF_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.104.2"

# report_type ("sr" / "pdf") → SOP Class UID.
_REPORT_SOP_CLASSES = {
    "sr": SR_SOP_CLASS,
    "pdf": PDF_SOP_CLASS,
}

# Study Root Query/Retrieve Information Model - FIND (supports STUDY, SERIES
# and IMAGE levels via the QueryRetrieveLevel field in the dataset).
_IMAGE_LEVEL_FIND_SOP = "1.2.840.10008.5.1.4.1.2.2.1"

_MAX_PDU_SIZE = 131072

# Association budget. Correction to review P1-12: pynetdicom 3.0.4 has no
# ``timeout=`` kwarg on associate() and its defaults (acse 30 s, network 60 s)
# already bound a peer that never answers — the "hangs the poller forever"
# failure does not occur on the pinned version. What was missing was
# configurability and visibility; the timeout is applied through the AE
# attributes (see _apply_timeouts) so a slow PACS can raise it from config.
_DEFAULT_ASSOCIATE_TIMEOUT_SEC = 30.0


class ReportFinderError(Exception):
    """Raised when the report C-FIND query cannot reach the PACS."""


@dataclass(frozen=True)
class ReportMatch:
    """A single report SOP instance returned by the C-FIND query."""

    sop_class_uid: str
    study_uid: str
    series_uid: str
    sop_instance_uid: str

    @property
    def report_type(self) -> str:
        """Return ``"sr"`` or ``"pdf"`` for the matched SOP class."""
        for report_type, sop in _REPORT_SOP_CLASSES.items():
            if sop == self.sop_class_uid:
                return report_type
        return "unknown"


class ReportFinder:
    """C-FIND SCU that locates report SOP instances on a PACS."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        aet: str,
        ae_title: str = "GATEWAY",
        timeout: float = _DEFAULT_ASSOCIATE_TIMEOUT_SEC,
    ) -> None:
        self.host = host
        self.port = port
        self.aet = aet
        self.ae_title = ae_title
        self.timeout = timeout

    def _apply_timeouts(self, ae: AE) -> None:
        """Bind the DIMSE budgets explicitly (see the module note on P1-12).

        ``connection_timeout`` is the TCP connect phase: it defaults to
        ``None`` (blocking) and is what ``AE.connect`` hands to
        ``settimeout`` before ``socket.connect()``, so without it a
        black-holed PACS host pins this thread until the OS stack times out.
        """
        ae.connection_timeout = self.timeout
        ae.acse_timeout = self.timeout
        ae.network_timeout = self.timeout

    def find(
        self,
        *,
        study_uid: str | None = None,
        accession: str | None = None,
        report_types: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ReportMatch]:
        """C-FIND the PACS for report instances matching the criteria.

        Returns a list of :class:`ReportMatch`; empty list when the PACS has
        no matching report instances.  Raises :class:`ReportFinderError` when
        the association cannot be established (PACS unreachable/rejected).

        ``limit`` caps the number of *matches* collected.  ``send_c_find``
        returns a lazy iterator, so once ``limit`` matches have been appended
        the loop breaks and the association is released in the ``finally``
        block — the SCP is never asked for results this caller will not read.
        A retrieval that only persists one instance (``_do_retrieve``) should
        pass ``limit=1`` rather than C-MOVE a whole study.  ``None`` (default)
        collects every match, preserving the behaviour of existing callers.
        """
        if not study_uid and not accession:
            raise ValueError("provide study_uid or accession")
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")
        report_types = report_types or ["sr", "pdf"]
        wanted_sops = {_REPORT_SOP_CLASSES[t] for t in report_types if t in _REPORT_SOP_CLASSES}

        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        if study_uid:
            query.StudyInstanceUID = study_uid
        if accession:
            query.AccessionNumber = accession
        query.SOPClassUID = ""
        query.SOPInstanceUID = ""
        query.SeriesInstanceUID = ""
        query.NumberOfSeriesRelatedInstances = ""

        ae = AE(ae_title=self.ae_title)
        ae.maximum_pdu_size = _MAX_PDU_SIZE
        self._apply_timeouts(ae)
        ae.add_requested_context(_IMAGE_LEVEL_FIND_SOP, ExplicitVRLittleEndian)

        assoc = ae.associate(self.host, self.port, ae_title=self.aet)
        if not assoc.is_established:
            raise ReportFinderError(
                f"C-FIND association rejected by PACS at {self.host}:{self.port}"
            )

        matches: list[ReportMatch] = []
        try:
            statuses = assoc.send_c_find(query, _IMAGE_LEVEL_FIND_SOP)
            for status, dataset in statuses:
                if status and status.Status == 0x0000:
                    break  # final Success — no more pending results
                if dataset is None:
                    continue
                sop_class = str(getattr(dataset, "SOPClassUID", ""))
                if wanted_sops and sop_class not in wanted_sops:
                    continue
                matches.append(
                    ReportMatch(
                        sop_class_uid=sop_class,
                        study_uid=str(
                            getattr(dataset, "StudyInstanceUID", study_uid or "")
                        ),
                        series_uid=str(getattr(dataset, "SeriesInstanceUID", "")),
                        sop_instance_uid=str(getattr(dataset, "SOPInstanceUID", "")),
                    )
                )
                if limit is not None and len(matches) >= limit:
                    # Early stop. send_c_find is a lazy generator: abandoning it
                    # here stops the SCP being interrogated for the remaining
                    # results, and the finally block below still releases the
                    # association. The 0x0000 Success break above stays the
                    # authority on normal termination — a Success status that
                    # arrives before the limit is hit still ends the query.
                    break
        finally:
            assoc.release()
        return matches
