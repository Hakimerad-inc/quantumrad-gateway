"""S08-T3 (RED): Report transport plugin surface (PRD §2.3, Q2).

Formalizes the S05 DICOM report retrieval as a pluggable ``ReportTransport``
protocol: a find step (C-FIND/QIDO) plus a retrieve step (C-MOVE/WADO).  A
registry dispatches by ``query_source.type`` so new transports (DICOMweb in
S08-T4) can be added without touching the poller state machine.

Behaviors:
1. Transports are registered under a ``type`` name and dispatched by it
2. An unknown type raises a clear error (no silent fallback)
3. The DICOM transport (S05) adapts the existing finder/mover and keeps
   SR/PDF report-type filtering intact
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mercure_gateway.config import ReportQuerySource
from mercure_gateway.reports.find import ReportMatch
from mercure_gateway.reports.move import RetrievedReport


@pytest.fixture(autouse=True)
def _clean_registry():
    from mercure_gateway.reports.transport import _REGISTRY, _reset_registry

    snapshot = dict(_REGISTRY)
    _reset_registry()
    yield
    _REGISTRY.update(snapshot)


# ══════════════════════════════════════════════════════════════════════
# Protocol conformance
# ══════════════════════════════════════════════════════════════════════

def test_dicom_transport_satisfies_protocol() -> None:
    """The DICOM transport exposes find() and retrieve() (protocol shape)."""
    from mercure_gateway.reports.transport import DICOMReportTransport

    transport = DICOMReportTransport(
        finder=_StubFinder(), retriever=_StubRetriever()
    )
    assert callable(transport.find)
    assert callable(transport.retrieve)


def test_dicom_transport_find_preserves_report_types() -> None:
    """find() forwards report_types so SR/PDF filtering is untouched (Q2)."""
    from mercure_gateway.reports.transport import DICOMReportTransport

    finder = _StubFinder()
    transport = DICOMReportTransport(finder=finder, retriever=_StubRetriever())

    transport.find(study_uid="1.2.3", report_types=["pdf"])

    assert finder.calls[0]["study_uid"] == "1.2.3"
    assert finder.calls[0]["report_types"] == ["pdf"]


def test_dicom_transport_retrieve_passthrough() -> None:
    """retrieve() delegates to the S05 C-MOVE mover."""
    from mercure_gateway.reports.transport import DICOMReportTransport

    retriever = _StubRetriever()
    match = ReportMatch(
        sop_class_uid="1.2.840.10008.5.1.4.1.1.88.33",
        study_uid="1.2.3",
        series_uid="1.2.3.4",
        sop_instance_uid="1.2.3.4.5",
    )
    transport = DICOMReportTransport(finder=_StubFinder(), retriever=retriever)

    saved = transport.retrieve([match])

    assert retriever.calls == [([match],)]
    assert saved[0].sop_class_uid == match.sop_class_uid


# ══════════════════════════════════════════════════════════════════════
# Registry dispatch
# ══════════════════════════════════════════════════════════════════════

def test_registry_dispatch_by_type() -> None:
    """A registered transport is returned for its type name."""
    from mercure_gateway.reports.transport import (
        register_transport,
        transport_for_type,
    )

    register_transport("fake", _FakeTransport)

    cls = transport_for_type("fake")
    assert cls is _FakeTransport


def test_registry_unknown_type_raises() -> None:
    """An unregistered type raises a clear error (no silent fallback)."""
    from mercure_gateway.reports.transport import transport_for_type

    with pytest.raises(KeyError, match="cda"):
        transport_for_type("cda")


def test_dicom_is_registered() -> None:
    """The S05 DICOM transport is pre-registered under ``"dicom"``."""
    from mercure_gateway.reports.transport import DICOMReportTransport, transport_for_type

    assert transport_for_type("dicom") is DICOMReportTransport


def test_factory_dispatch_from_query_source() -> None:
    """transport_for_query_source() builds from a ReportQuerySource."""
    from mercure_gateway.reports.transport import (
        _StubFactory,
        register_factory,
        transport_for_query_source,
    )

    register_factory("dicomweb", _StubFactory())
    qs = ReportQuerySource(type="dicomweb", host="pacs.local", port=104, aet="PACS")

    transport = transport_for_query_source(qs)

    assert transport is not None
    assert transport["source"] is qs


def test_factory_unknown_type_raises() -> None:
    """An unknown query_source.type raises a clear error."""
    from mercure_gateway.reports.transport import transport_for_query_source

    qs = ReportQuerySource(type="hl7", host="hub.local", port=443, aet="PACS")

    with pytest.raises(KeyError, match="hl7"):
        transport_for_query_source(qs)


# ══════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════


class _StubFinder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def find(self, **kw: Any) -> list[ReportMatch]:
        self.calls.append(kw)
        return []


class _StubRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[list[ReportMatch]]] = []

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        self.calls.append((matches,))
        return [
            RetrievedReport(
                sop_class_uid=m.sop_class_uid,
                study_uid=m.study_uid,
                sop_instance_uid=m.sop_instance_uid,
                file_path=Path(f"reports/{m.study_uid}/sr.dcm"),
            )
            for m in matches
        ]


class _FakeTransport:
    """Minimal protocol-compatible transport for registry tests."""

    def find(self, **kw: Any) -> list[ReportMatch]:
        return []

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        return []
