"""Report transport plugin surface (PRD §2.3, Q2, S08-T3).

Formalizes the Sprint 05 DICOM report retrieval (C-FIND/C-MOVE) behind a
pluggable ``ReportTransport`` protocol so additional transports (DICOMweb
QIDO/WADO in S08-T4, experimental HL7/FHIR in S08-T5) can be added without
touching the poller state machine.

A transport is a find step (locate report instances) plus a retrieve step
(pull the matching instances and save them to disk).  Registration is keyed by
``query_source.type`` so the composition root can build the transport directly
from configuration::

    transport = transport_for_query_source(config.reports.query_source)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from mercure_gateway.config import ReportQuerySource
from mercure_gateway.reports.find import ReportFinder, ReportMatch
from mercure_gateway.reports.move import ReportRetrieve, RetrievedReport

__all__ = [
    "DICOMReportTransport",
    "ReportTransport",
    "register_factory",
    "register_transport",
    "transport_for_query_source",
    "transport_for_type",
]

# Registry of transport *classes* keyed by type name (used when no custom
# factory is registered for that type).
_REGISTRY: dict[str, type[Any]] = {}
# Optional per-type factories that build a fully-wired transport from the
# query source (e.g. DICOM transport needs reports_dir + store_scp_port).
_FACTORIES: dict[str, Any] = {}


class ReportTransport(Protocol):
    """A pluggable report retrieval transport.

    Implementations must expose ``find`` (locate report instances on the
    remote) and ``retrieve`` (pull the matching instances to disk).  This is
    the seam that DICOMweb QIDO/WADO and future transports implement.
    """

    def find(
        self,
        *,
        study_uid: str | None = None,
        accession: str | None = None,
        report_types: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ReportMatch]:
        """Locate report instances matching the criteria.

        ``limit`` caps how many matches the transport collects before it stops
        interrogating the remote (``None`` = unbounded).  Transports map it to
        whatever their protocol supports — the DICOM C-FIND implementation
        abandons the lazy result iterator, DICOMweb sends it as ``_limit``.
        """
        ...

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        """Pull the matched report instances and save them to disk."""
        ...


@dataclass
class _TransportSpec:
    """A registered transport plus optional custom factory."""

    cls: type[Any]
    factory: Any | None = None


def register_transport(type_name: str, cls: type[Any]) -> None:
    """Register a transport class under ``type_name`` (e.g. ``"dicom"``)."""
    _REGISTRY[type_name] = cls


def register_factory(type_name: str, factory: Any) -> None:
    """Register a transport factory callable under ``type_name``.

    The factory receives the :class:`ReportQuerySource` and returns a
    fully-wired :class:`ReportTransport`.
    """
    _FACTORIES[type_name] = factory


def transport_for_type(type_name: str) -> type[Any]:
    """Return the registered transport class for ``type_name``.

    Raises ``KeyError`` for an unregistered type — a silent fallback would
    hide misconfiguration (a report silently never retrieved).
    """
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise KeyError(f"no report transport registered for type {type_name!r}") from None


def transport_for_query_source(source: ReportQuerySource) -> ReportTransport:
    """Build a transport for ``source.type`` using its registered factory.

    When no custom factory is registered, the transport class is instantiated
    with the query source fields (host/port/aet).  Raises ``KeyError`` for an
    unknown type.
    """
    factory = _FACTORIES.get(source.type)
    if factory is not None:
        return factory(source)  # type: ignore[no-any-return]
    cls = transport_for_type(source.type)
    return cls(source)  # type: ignore[no-any-return]


class DICOMReportTransport:
    """S05 DICOM transport: adapts ReportFinder + ReportRetrieve.

    Built from a :class:`ReportQuerySource` (host/port/aet) plus the gateway's
    internal C-STORE SCP port and reports directory.  ``find`` delegates to
    :class:`ReportFinder` and ``retrieve`` to :class:`ReportRetrieve`, keeping
    the SR/PDF SOP-class filtering behavior unchanged.
    """

    def __init__(
        self,
        finder: ReportFinder | Any,
        retriever: ReportRetrieve | Any,
    ) -> None:
        self._finder = finder
        self._retriever = retriever

    def find(
        self,
        *,
        study_uid: str | None = None,
        accession: str | None = None,
        report_types: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ReportMatch]:
        return self._finder.find(
            study_uid=study_uid,
            accession=accession,
            report_types=report_types,
            limit=limit,
        )

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        return self._retriever.retrieve(matches)


def _register_defaults() -> None:
    """Register the built-in transports (idempotent; re-called on reset)."""
    register_transport("dicom", DICOMReportTransport)
    from mercure_gateway.reports.dicomweb import DICOMwebReportTransport
    from mercure_gateway.reports.hl7_fhir import HL7FHIRTransport

    register_transport("dicomweb", DICOMwebReportTransport)
    register_transport("fhir", HL7FHIRTransport)


def _reset_registry() -> None:
    """Clear the registries and restore the built-in transports (tests)."""
    _REGISTRY.clear()
    _FACTORIES.clear()
    _register_defaults()


class _StubFactory:
    """Test double: returns the query source it was built from."""

    def __call__(self, source: ReportQuerySource) -> dict[str, Any]:
        return {"source": source}


# Pre-register the built-in transports.
_register_defaults()
