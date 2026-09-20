"""S05-T1 (RED): Report query transport — C-FIND SCU (US-05, refinement §2.3).

Queries a PACS by StudyInstanceUID or AccessionNumber, filtering by SOP Class
UID to distinguish DICOM SR (1.2.840.10008.5.1.4.1.1.88.33) from Encapsulated
PDF (1.2.840.10008.5.1.4.1.1.104.2).

Behaviors:
1. C-FIND for SR returns only SR SOP instances
2. C-FIND for PDF returns only PDF SOP instances
3. C-FIND with no match returns empty list
4. Connection error raises ConnectionError
5. SOP class filter works (requesting SR only returns SR, not PDF)
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_SR_SOP = "1.2.840.10008.5.1.4.1.1.88.33"
_PDF_SOP = "1.2.840.10008.5.1.4.1.1.104.2"
_IMAGE_FIND_SOP = "1.2.840.10008.5.1.4.1.2.2.1"  # Study Root Q/R FIND


def _find_scp_ae(port: int, *results: Dataset) -> AE:
    """Create a C-FIND SCP that returns *results* for any IMAGE-level query."""
    ae = AE(ae_title="FINDSCP")
    ae.add_supported_context(_IMAGE_FIND_SOP, ExplicitVRLittleEndian)

    def on_c_find(event: evt.Event) -> Any:
        for ds in results:
            yield (0xFF00, ds)
        yield (0x0000, None)

    ae.start_server(("", port), evt_handlers=[(evt.EVT_C_FIND, on_c_find)], block=False)
    return ae


def _make_sr_instance(study_uid: str, sop_uid: str | None = None) -> Dataset:
    ds = Dataset()
    ds.QueryRetrieveLevel = "IMAGE"
    ds.StudyInstanceUID = study_uid
    ds.SOPInstanceUID = sop_uid or "1.2.3.4.5.6.7.1"
    ds.SOPClassUID = _SR_SOP
    ds.SeriesInstanceUID = "1.2.3.4.5.6.8"
    ds.NumberOfSeriesRelatedInstances = "1"
    return ds


def _make_pdf_instance(study_uid: str, sop_uid: str | None = None) -> Dataset:
    ds = Dataset()
    ds.QueryRetrieveLevel = "IMAGE"
    ds.StudyInstanceUID = study_uid
    ds.SOPInstanceUID = sop_uid or "1.2.3.4.5.6.7.2"
    ds.SOPClassUID = _PDF_SOP
    ds.SeriesInstanceUID = "1.2.3.4.5.6.9"
    ds.NumberOfSeriesRelatedInstances = "1"
    return ds


def test_find_sr_returns_sr_instances() -> None:
    from mercure_gateway.reports.find import ReportFinder

    sr = _make_sr_instance("1.2.840.1")
    port = free_port()
    scp = _find_scp_ae(port, sr)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"])
        assert len(results) == 1
        assert results[0].sop_class_uid == _SR_SOP
        assert results[0].study_uid == "1.2.840.1"
    finally:
        scp.shutdown()


def test_find_pdf_returns_pdf_instances() -> None:
    from mercure_gateway.reports.find import ReportFinder

    pdf = _make_pdf_instance("1.2.840.1")
    port = free_port()
    scp = _find_scp_ae(port, pdf)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        results = finder.find(study_uid="1.2.840.1", report_types=["pdf"])
        assert len(results) == 1
        assert results[0].sop_class_uid == _PDF_SOP
    finally:
        scp.shutdown()


def test_find_with_no_match_returns_empty() -> None:
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    ae = AE(ae_title="FINDSCP")
    ae.add_supported_context(_IMAGE_FIND_SOP, ExplicitVRLittleEndian)

    def on_empty(event: evt.Event) -> Any:
        yield (0x0000, None)  # Success — no matches

    ae.start_server(("", port), evt_handlers=[(evt.EVT_C_FIND, on_empty)], block=False)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        results = finder.find(study_uid="1.2.3.4", report_types=["sr"])
        assert results == []
    finally:
        ae.shutdown()


def test_find_sop_class_filter_works() -> None:
    """Requesting SR only returns SR instances, even when PDF instances exist."""
    from mercure_gateway.reports.find import ReportFinder

    sr = _make_sr_instance("1.2.840.1", "1.2.3.4.5.6.7.1")
    pdf = _make_pdf_instance("1.2.840.1", "1.2.3.4.5.6.7.2")
    port = free_port()
    scp = _find_scp_ae(port, sr, pdf)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        sr_only = finder.find(study_uid="1.2.840.1", report_types=["sr"])
        assert len(sr_only) == 1
        assert sr_only[0].sop_class_uid == _SR_SOP

        both = finder.find(study_uid="1.2.840.1", report_types=["sr", "pdf"])
        assert len(both) == 2
    finally:
        scp.shutdown()


def test_connection_error_raises() -> None:
    from mercure_gateway.reports.find import ReportFinder, ReportFinderError

    port = free_port()  # nothing listening on this port
    finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
    with pytest.raises(ReportFinderError):
        finder.find(study_uid="1.2.3.4", report_types=["sr"])


# ── Association budget (review P1-12) ───────────────────────────────────


def test_timeouts_are_applied_to_the_ae() -> None:
    """The C-FIND budget is bound explicitly rather than left implicit.

    pynetdicom 3.0.4 has no ``timeout=`` kwarg on ``associate()``; the budget
    is set on the AE instance so a slow PACS can raise it from config.
    """
    from pynetdicom import AE

    from mercure_gateway.reports.find import (
        ReportFinder,
    )

    finder = ReportFinder(host="127.0.0.1", port=11112, aet="PACS", timeout=90.0)
    ae = AE(ae_title="GATEWAY")
    finder._apply_timeouts(ae)

    assert ae.connection_timeout == 90.0
    assert ae.acse_timeout == 90.0
    assert ae.network_timeout == 90.0


def test_timeouts_fall_back_to_the_default() -> None:
    from pynetdicom import AE

    from mercure_gateway.reports.find import (
        _DEFAULT_ASSOCIATE_TIMEOUT_SEC,
        ReportFinder,
    )

    finder = ReportFinder(host="127.0.0.1", port=11112, aet="PACS")
    ae = AE(ae_title="GATEWAY")
    finder._apply_timeouts(ae)

    assert ae.connection_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
    assert ae.acse_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
    assert ae.network_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
