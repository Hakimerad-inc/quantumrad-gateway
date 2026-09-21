"""HL7/FHIR report transport (experimental, PRD §2.3, S08-T5).

.. warning::
   This transport is **experimental** and feature-flagged off by default.
   ORU^R01-to-folder / email-to-folder style report drop is not a
   production-ready retrieval path in v1.  Enable only in controlled
   evaluation environments.

When enabled, the transport listens for HL7 v2 ORU^R01 ADT messages or
FHIR DocumentReference resources and stores them under
``reports/{study_uid}/hl7/``.  The ``find`` / ``retrieve`` protocol
methods raise :class:`NotImplementedError` — the transport is a contract
placeholder for future work.
"""

from __future__ import annotations

from mercure_gateway.reports.find import ReportMatch
from mercure_gateway.reports.move import RetrievedReport

__all__ = ["HL7FHIRTransport"]

# Feature-flag: off by default in v1.
ENABLED = False


class HL7FHIRTransport:
    """Experimental HL7/FHIR report drop transport (feature-flagged off).

    Satisfies the :class:`~mercure_gateway.reports.transport.ReportTransport`
    protocol shape but raises ``NotImplementedError`` until the feature is
    developed (S08-Q5).  The optional ``source`` argument is accepted so the
    transport can be built uniformly by ``transport_for_query_source``.
    """

    def __init__(self, source: object | None = None) -> None:
        self._source = source

    def find(
        self,
        *,
        study_uid: str | None = None,
        accession: str | None = None,
        report_types: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ReportMatch]:
        # ``limit`` is accepted to keep the ReportTransport signature uniform
        # (see reports.transport): HL7 v2 ORU^R01 is a push protocol with no
        # server-side query to bound, so there is nothing to do with it here.
        raise NotImplementedError(
            "HL7/FHIR report transport is experimental and not yet implemented"
        )

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        raise NotImplementedError(
            "HL7/FHIR report transport is experimental and not yet implemented"
        )
