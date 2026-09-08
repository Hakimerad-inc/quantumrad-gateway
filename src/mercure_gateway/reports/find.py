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
    ) -> None:
        self.host = host
        self.port = port
        self.aet = aet
        self.ae_title = ae_title

    def find(
        self,
        *,
        study_uid: str | None = None,
        accession: str | None = None,
        report_types: list[str] | None = None,
    ) -> list[ReportMatch]:
        """C-FIND the PACS for report instances matching the criteria.

        Returns a list of :class:`ReportMatch`; empty list when the PACS has
        no matching report instances.  Raises :class:`ReportFinderError` when
        the association cannot be established (PACS unreachable/rejected).
        """
        if not study_uid and not accession:
            raise ValueError("provide study_uid or accession")
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
        finally:
            assoc.release()
        return matches
